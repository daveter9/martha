"""Stand-in for paho.mqtt.publish. Grott imports it even with nomqtt = True; martha has no
MQTT broker (C6), so publishing is an error."""


def single(*args, **kwargs):
    raise RuntimeError("MQTT is not available on martha; set nomqtt = True in grott.ini")
