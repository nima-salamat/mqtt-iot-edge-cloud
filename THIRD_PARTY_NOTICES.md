# Third-party notices and model licensing

## YOLOv5n NCNN model

The Raspberry Pi installer downloads the pinned model from:
- Repository: https://github.com/nihui/ncnn-webassembly-yolov5
- Revision: 85e5a7cd8236b71391a3f7ffd021a076f102024c

The installer verifies upstream Git blob SHA-1 hashes and exact file sizes before installing
the parameter and weight files. Binary model assets are downloaded on the Pi and are not
committed to this repository.

The model is derived from Ultralytics YOLOv5 and its COCO classes (class 0 is person).
Ultralytics YOLOv5 is distributed under AGPL-3.0. Review the upstream license and its
obligations before hosting or distributing this project, especially for commercial or
network-accessible deployments:
https://github.com/ultralytics/yolov5

## NCNN

NCNN is the neural-network inference framework from Tencent:
https://github.com/Tencent/ncnn

The Pi installer installs NCNN Python bindings. It does not vendor NCNN source into this
repository.

## Other runtime components

The project also uses Django, Streamlit, Paho MQTT, Flask, Waitress, Picamera2, PyAV,
OpenCV, Mosquitto, PostgreSQL and MediaMTX. Each remains subject to its own notices.
