# YOLOv5n NCNN weights

The install script downloads these two NCNN model artifacts into this folder:

- yolov5n.ncnn.param
- yolov5n.ncnn.bin

Source: nihui/ncnn-webassembly-yolov5, revision 85e5a7cd8236b71391a3f7ffd021a076f102024c.

Each file is checked against its upstream Git blob SHA-1 and exact byte size before installation. Altered or incomplete downloads are rejected.

Run from the repository root on Raspberry Pi OS:

    sudo ./scripts/install_pi.sh

The binary weight files are fetched during installation rather than stored in this Git repository. The installer puts them under /opt/mqtt-iot-edge-cloud/pi_device/models.
