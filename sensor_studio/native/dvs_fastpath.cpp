#include <algorithm>
#include <atomic>
#include <cstdint>
#include <limits>
#include <mutex>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

#include <pybind11/numpy.h>
#include <pybind11/pybind11.h>

#include "metavision/hal/facilities/i_event_decoder.h"
#include "metavision/sdk/base/events/event_cd.h"

namespace py = pybind11;

namespace {

class DvsFrameBuilder {
public:
    DvsFrameBuilder(int width, int height, int accumulation_us = 10000)
        : width_(width),
          height_(height),
          accumulation_us_(accumulation_us),
          last_timestamp_us_(static_cast<size_t>(width) * height,
                             std::numeric_limits<int64_t>::min()),
          last_polarity_(static_cast<size_t>(width) * height, -1) {
        if (width <= 0 || height <= 0) {
            throw std::invalid_argument("DVS width and height must be positive");
        }
        if (accumulation_us <= 0) {
            throw std::invalid_argument("DVS accumulation time must be positive");
        }
    }

    uint64_t attach(py::object &decoder_object) {
        std::lock_guard<std::mutex> lock(control_mutex_);
        if (attached_) {
            throw std::runtime_error("DVS native callback is already attached");
        }
        auto &decoder =
            decoder_object.cast<Metavision::I_EventDecoder<Metavision::EventCD> &>();
        callback_id_ = decoder.add_event_buffer_callback(
            [this](const Metavision::EventCD *begin,
                   const Metavision::EventCD *end) {
                consume_events(begin, end);
            });
        attached_ = true;
        return callback_id_;
    }

    void detach(py::object &decoder_object) {
        uint64_t callback_id = 0;
        {
            std::lock_guard<std::mutex> lock(control_mutex_);
            if (!attached_) {
                return;
            }
            callback_id = callback_id_;
            callback_id_ = 0;
            attached_ = false;
        }
        auto &decoder =
            decoder_object.cast<Metavision::I_EventDecoder<Metavision::EventCD> &>();
        decoder.remove_callback(callback_id);
    }

    py::array_t<uint8_t> latest_frame() {
        py::array_t<uint8_t> output(
            {height_, width_, 3},
            {static_cast<py::ssize_t>(width_ * 3),
             static_cast<py::ssize_t>(3),
             static_cast<py::ssize_t>(1)});
        auto frame = output.mutable_unchecked<3>();

        std::lock_guard<std::mutex> lock(event_mutex_);
        const int64_t cutoff_us = latest_timestamp_us_ - accumulation_us_;
        for (int y = 0; y < height_; ++y) {
            for (int x = 0; x < width_; ++x) {
                const size_t index =
                    static_cast<size_t>(y) * width_ + static_cast<size_t>(x);
                uint8_t b = 30;
                uint8_t g = 37;
                uint8_t r = 52;
                if (last_timestamp_us_[index] >= cutoff_us) {
                    if (last_polarity_[index] > 0) {
                        b = 255;
                        g = 255;
                        r = 255;
                    } else if (last_polarity_[index] == 0) {
                        b = 200;
                        g = 126;
                        r = 64;
                    }
                }
                frame(y, x, 0) = b;
                frame(y, x, 1) = g;
                frame(y, x, 2) = r;
            }
        }
        return output;
    }

    void consume_test(py::array_t<int32_t, py::array::c_style | py::array::forcecast> x,
                      py::array_t<int32_t, py::array::c_style | py::array::forcecast> y,
                      py::array_t<int32_t, py::array::c_style | py::array::forcecast> polarity,
                      py::array_t<int64_t, py::array::c_style | py::array::forcecast>
                          timestamp_us) {
        const py::ssize_t size = x.size();
        if (y.size() != size || polarity.size() != size ||
            timestamp_us.size() != size) {
            throw std::invalid_argument("all event arrays must have equal length");
        }
        auto x_values = x.unchecked<1>();
        auto y_values = y.unchecked<1>();
        auto polarity_values = polarity.unchecked<1>();
        auto timestamp_values = timestamp_us.unchecked<1>();

        callback_count_.fetch_add(1, std::memory_order_relaxed);
        event_count_.fetch_add(static_cast<uint64_t>(size),
                               std::memory_order_relaxed);
        std::lock_guard<std::mutex> lock(event_mutex_);
        for (py::ssize_t i = 0; i < size; ++i) {
            consume_event(x_values(i), y_values(i), polarity_values(i),
                          timestamp_values(i));
        }
    }

    py::dict stats() const {
        py::dict result;
        result["callback_count"] = callback_count_.load();
        result["event_count"] = event_count_.load();
        result["width"] = width_;
        result["height"] = height_;
        result["accumulation_us"] = accumulation_us_;
        return result;
    }

    void reset_stats() {
        callback_count_.store(0, std::memory_order_relaxed);
        event_count_.store(0, std::memory_order_relaxed);
    }

private:
    void consume_events(const Metavision::EventCD *begin,
                        const Metavision::EventCD *end) {
        const uint64_t count = static_cast<uint64_t>(end - begin);
        callback_count_.fetch_add(1, std::memory_order_relaxed);
        event_count_.fetch_add(count, std::memory_order_relaxed);
        if (count == 0) {
            return;
        }

        std::lock_guard<std::mutex> lock(event_mutex_);
        for (const auto *event = begin; event != end; ++event) {
            consume_event(event->x, event->y, event->p, event->t);
        }
    }

    void consume_event(int64_t x, int64_t y, int64_t polarity,
                       int64_t timestamp_us) {
        if (x < 0 || x >= width_ || y < 0 || y >= height_) {
            return;
        }
        const size_t index =
            static_cast<size_t>(y) * width_ + static_cast<size_t>(x);
        last_timestamp_us_[index] = timestamp_us;
        last_polarity_[index] = static_cast<int8_t>(polarity > 0 ? 1 : 0);
        latest_timestamp_us_ = std::max(latest_timestamp_us_, timestamp_us);
    }

    const int width_;
    const int height_;
    const int accumulation_us_;
    std::vector<int64_t> last_timestamp_us_;
    std::vector<int8_t> last_polarity_;
    int64_t latest_timestamp_us_ = std::numeric_limits<int64_t>::min();
    std::atomic<uint64_t> callback_count_{0};
    std::atomic<uint64_t> event_count_{0};
    mutable std::mutex event_mutex_;
    std::mutex control_mutex_;
    uint64_t callback_id_ = 0;
    bool attached_ = false;
};

}  // namespace

PYBIND11_MODULE(_dvs_fastpath, module) {
    module.doc() =
        "Low-overhead DVS event callback and preview renderer for Sensor Studio";
    py::class_<DvsFrameBuilder>(module, "DvsFrameBuilder")
        .def(py::init<int, int, int>(), py::arg("width"), py::arg("height"),
             py::arg("accumulation_us") = 10000)
        .def("attach", &DvsFrameBuilder::attach, py::arg("event_decoder"))
        .def("detach", &DvsFrameBuilder::detach, py::arg("event_decoder"))
        .def("latest_frame", &DvsFrameBuilder::latest_frame)
        .def("consume_test", &DvsFrameBuilder::consume_test, py::arg("x"),
             py::arg("y"), py::arg("polarity"), py::arg("timestamp_us"))
        .def("reset_stats", &DvsFrameBuilder::reset_stats)
        .def("stats", &DvsFrameBuilder::stats);
}
