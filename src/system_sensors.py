#!/usr/bin/env python3
import argparse
import datetime as dt
import signal
import sys
import socket
import platform
import threading
import time
from datetime import timedelta
from re import findall
import re
from subprocess import check_output
import paho.mqtt.client as mqtt
import psutil
import pytz
import yaml
import csv
import websocket
import json
from os import path
from pytz import timezone

try:
    import apt

    apt_enabled = True  # I switched this variable around to use a positive boolean, this can help readability when trying to determine complex situations downstream
except ImportError:
    apt_enabled = False


class ProgramKilled(Exception):
    pass


def signal_handler(signum, frame):
    raise ProgramKilled


class Job(threading.Thread):
    def __init__(self, interval, execute, *args, **kwargs):
        threading.Thread.__init__(self)
        self.daemon = False
        self.stopped = threading.Event()
        self.interval = interval
        self.execute = execute
        self.args = args
        self.kwargs = kwargs

    def stop(self):
        self.stopped.set()
        self.join()

    def run(self):
        while not self.stopped.wait(self.interval.total_seconds()):
            self.execute(*self.args, **self.kwargs)


def print_flush(message):
    print(message)
    sys.stdout.flush()


def utc_from_timestamp(timestamp: float) -> dt.datetime:
    """Return a UTC time from a timestamp."""
    return pytz.utc.localize(dt.datetime.utcfromtimestamp(timestamp))


def as_local(dattim: dt.datetime) -> dt.datetime:
    """Convert a UTC datetime object to local time zone."""
    if dattim.tzinfo == DEFAULT_TIME_ZONE:
        return dattim
    if dattim.tzinfo is None:
        dattim = pytz.utc.localize(dattim)

    return dattim.astimezone(DEFAULT_TIME_ZONE)


def get_last_boot():
    return str(as_local(utc_from_timestamp(psutil.boot_time())).isoformat())


def get_last_message():
    return str(as_local(utc_from_timestamp(time.time())).isoformat())


def _on_message(client, userdata, message):
    print(f"Message received: {message.payload.decode()}")
    if message.payload.decode() == "online":
        send_config_message(client)


previousResponse = False
rust_stat_fails = 0

def on_message(client, userdata, message):
    print (f"Message received: {message.payload.decode()}"  )

    if message.payload.decode() == "online":
        send_config_message(client)
    elif message.payload.decode() == "display_on":
        reading = check_output(["echo", "\"standby 0\"", "|", "cec-client", "-s", "-d", "0"]).decode("UTF-8")
        update_sensors()
    elif message.payload.decode() == "display_off":
        reading = check_output(["echo", "\"on 0\"", "|", "cec-client", "-s", "-d", "0"]).decode("UTF-8")
        update_sensors()

# adjusted function name to match standard Python conventions. :)
def update_sensors():
    global previousResponse
    global rust_stat_fails
    print_flush("Updating sensors...")
    network = get_network_usage()
    payload = {
        "temperature": get_temp(),
        "disk_use": get_disk_usage("/"),
        "memory_use": get_memory_usage(),
        "cpu_usage": get_cpu_usage(),
        "swap_usage": get_swap_usage(),
        "last_boot": get_last_boot(),
        "last_message": get_last_message(),
        "hostname": get_host_name(),
        "host_ip": get_host_ip(),
        "host_os": get_host_os(),
        "network_out": network["network_out_speed"],
        "network_in": network["network_in_speed"],
        "host_arch": get_host_arch(),
    }

    if "rasp" in OS_DATA["ID"]:
        payload["display"] = get_display_status()

    if settings.get("check_available_updates") and apt_enabled:
        payload["updates"] = get_updates()

    if settings.get("enable_rust_server"):
        response = get_rust_server_info(
            settings.get("rust_server_ip", "localhost"),
            settings.get("rust_rcon_port", 28016),
            settings["rcon_password"],
        )

        rust_online_status = True
        if "MaxPlayers" not in response:
            print_flush(
                "Error fetching server info, it might be down or something else... I dunnow..."
            )
            response = previousResponse
            rust_stat_fails += 1
            rust_online_status = False

        if rust_stat_fails == 3:
            mqttClient.publish(
                f"system-sensors/sensor/{deviceName}/rust_server_availability",
                "offline",
                retain=True,
            )
            print_flush("Offline")

        elif rust_online_status == True and rust_stat_fails > 0:
            rust_stat_fails = 0
            print_flush("back online")
            mqttClient.publish(
                f"system-sensors/sensor/{deviceName}/rust_server_availability",
                "online",
                retain=True,
            )

        if response:
            payload["rust_server_max_players"] = response["MaxPlayers"]
            payload["rust_server_players"] = response["Players"]
            payload["rust_server_players_queued"] = response["Queued"]
            payload["rust_server_players_joining"] = response["Joining"]
            payload["rust_server_entity_count"] = response["EntityCount"]
            payload["rust_server_framerate"] = response["Framerate"]
            payload["rust_server_memory"] = response["Memory"]
            payload["rust_server_network_in"] = response["NetworkIn"] / 1024
            payload["rust_server_network_out"] = response["NetworkOut"] / 1024
            previousResponse = response

    if settings.get("check_wifi_strength"):
        payload["wifi_strength"] = get_wifi_strength()
    if "external_drives" in settings:
        for drive in settings["external_drives"]:
            payload[f"disk_use_{drive.lower()}"] = get_disk_usage(
                settings["external_drives"][drive]
            )

    payload_str = json.dumps(payload)
    print_flush(payload_str)
    mqttClient.publish(
        topic=f"system-sensors/sensor/{deviceName}/state",
        payload=payload_str,
        qos=1,
        retain=False,
    )


def get_updates():
    cache = apt.Cache()
    cache.open(None)
    cache.upgrade()
    return str(cache.get_changes().__len__())


# Temperature method depending on system distro
def get_temp():
    if "rasp" in OS_DATA.get("ID", {}):
        reading = check_output(["vcgencmd", "measure_temp"]).decode("UTF-8")
        return str(findall("\d+\.\d+", reading)[0])

    if path.exists("/sys/class/thermal/thermal_zone0"):
        reading = check_output(["cat", "/sys/class/thermal/thermal_zone0/temp"]).decode(
            "UTF-8"
        )
        return str(reading[0] + reading[1] + "." + reading[2])
    return 0.0

def get_display_status():
    display_state = ""
    if "rasp" in OS_DATA["ID"]:
        reading = check_output(["vcgencmd", "display_power"]).decode("UTF-8")
        display_state = str(re.findall("^display_power=(?P<display_state>[01]{1})$", reading)[0])
    else:
        display_state = "Unknown"
    return display_state

def get_disk_usage(path):
    return str(psutil.disk_usage(path).percent)


def get_memory_usage():
    return str(psutil.virtual_memory().percent)


def get_cpu_usage():
    return str(psutil.cpu_percent(interval=None))


def get_swap_usage():
    return str(psutil.swap_memory().percent)


network_stats_prev = psutil.net_io_counters()
network_stats_prev_time = time.time()


def get_network_usage():
    global network_stats_prev
    global network_stats_prev_time
    network_stats = psutil.net_io_counters()
    # time.sleep(1)
    # network_stats2 = psutil.net_io_counters()

    now = time.time()
    time_diff = now - network_stats_prev_time

    byte_diff_rx = (
        network_stats.bytes_recv - network_stats_prev.bytes_recv
    ) / time_diff
    byte_diff_tx = (
        network_stats.bytes_sent - network_stats_prev.bytes_sent
    ) / time_diff
    tx_speed = round(byte_diff_tx / 1024, 2)
    rx_speed = round(byte_diff_rx / 1024, 2)

    result = dict()
    result["network_out_speed"] = float(tx_speed)
    result["network_in_speed"] = float(rx_speed)
    return result


def get_wifi_strength():  # check_output(["/proc/net/wireless", "grep wlan0"])
    wifi_strength_value = (
        check_output(
            [
                "bash",
                "-c",
                "cat /proc/net/wireless | grep wlan0: | awk '{print int($4)}'",
            ]
        )
        .decode("utf-8")
        .rstrip()
    )
    return (
        wifi_strength_value or "0"
    )  # You can 'or' between values when you want to use a fallback


def get_host_name():
    return socket.gethostname()


def get_host_ip():
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except socket.error:
        try:
            return socket.gethostbyname(socket.gethostname())
        except socket.gaierror:
            return "127.0.0.1"
    finally:
        sock.close()


def get_host_os():
    return OS_DATA.get("PRETTY_NAME") or "Unknown"


def get_host_arch():
    try:
        return platform.machine()
    except:
        return "Unknown"


def get_rust_server_info(ip, port, password):
    server_uri = "ws://{0}:{1}/{2}".format(ip, port, password)
    command_json = {}
    command_json["Identifier"] = 1
    command_json["Message"] = "serverinfo"
    command_json["Name"] = "WebRcon"
    command_json = json.dumps(command_json)
    ws = websocket.WebSocket()

    try:
        ws.connect(server_uri)
        ws.send(command_json)
        response = ws.recv()
        ws.close()
        response = json.loads(response.replace("\n", ""))
        return json.loads(response["Message"])
    except Exception as e:
        # Inform the user it was a failure to connect. provide Exception string for further diagnostics.
        return f"Failed to connect. {str(e)}"


# I adjusted this function to minimize repeated sections of code
def remove_old_topics(client, device_display_name, external_drives=None):
    if not external_drives:
        external_drives = []

    topic_words = [
        "Temp",
        "DiskUse",
        "MemoryUse",
        "CpuUsage",
        "SwapUsage",
        "PowerStatus",
        "LastBoot",
        "LastMessage",
        "WifiStrength",
        "Updates",
    ]
    topics = [
        f"homeassistant/sensor/{device_display_name}/{device_display_name}{word}/config"
        for word in topic_words
    ]
    for drive in external_drives:
        topics.append(
            f"homeassistant/sensor/{device_display_name}/{device_display_name}DiskUse{drive}/config"
        )

    if "rasp" in OS_DATA["ID"]:           
        topics.append(
            f"homeassistant/switch/{device_display_name}/{device_display_name}Display/config"
        )
    for x in topics:
        client.publish(topic=x, payload="", qos=1, retain=False)


# Since the settings seem to be an all-or-nothing setup, the code now checks all conditions and reports all errors.
def check_settings(settings):
    messages = []
    if "mqtt" not in settings:
        messages.append(
            "Mqtt not defined in settings.yaml! Please check the documentation"
        )
    if "hostname" not in settings["mqtt"]:
        messages.append(
            "Mqtt not defined in settings.yaml! Please check the documentation"
        )
    if "hostname" not in settings["mqtt"]:
        messages.append(
            "Hostname not defined in settings.yaml! Please check the documentation"
        )
    if "timezone" not in settings:
        messages.append(
            "Timezone not defined in settings.yaml! Please check the documentation"
        )
    if "deviceName" not in settings:
        messages.append(
            "deviceName not defined in settings.yaml! Please check the documentation"
        )
    if "client_id" not in settings:
        messages.append(
            "client_id not defined in settings.yaml! Please check the documentation"
        )
    if "power_integer_state" in settings:
        messages.append(
            "power_integer_state is deprecated please remove this option power state is now a binary_sensor!"
        )

    for message in messages:
        print_flush(message)
    if messages:
        sys.exit()


class Message:
    def __init__(
        self,
        device_name,
        device_display_name,
        device_model,
        device_manufacturer,
        snake_name,
        name_suffix,
        icon,
        device_class=None,
        unit_of_measurement=None,
        availability_topic="availability",
        unique_id_prefix="sensor_",
    ):
        self.device_name = device_name
        self.device_display_name = device_display_name
        self.device_manufacturer = device_manufacturer
        self.device_display_model = device_model
        self.snake_name = snake_name
        self.name_suffix = name_suffix
        self.icon = icon

        self.device_class = device_class
        self.unit_of_measurement = unit_of_measurement
        self.availability_topic = availability_topic
        self.unique_id_prefix = unique_id_prefix

    def to_dict(self):
        device_dict = {
            "identifiers": [f"{self.device_name}_sensor"],
            "name": f"{self.device_display_name}",
            "model": f"{self.device_display_model}",
            "manufacturer": f"{self.device_manufacturer}",
        }

        payload = {
            "name": f"{self.device_display_name} {self.name_suffix}",
            "state_topic": _state_topic(self.device_name),
            "value_template": _value_json_name(self.snake_name),
            "unique_id": _unique_id(
                self.device_name, f"{self.unique_id_prefix}{self.snake_name}"
            ),
            "availability_topic": f"system-sensors/sensor/{self.device_name}/{self.availability_topic}",
            "device": device_dict,
            "icon": self.icon,
        }
        if self.device_class:
            payload["device_class"] = self.device_class
        if self.unit_of_measurement:
            payload["unit_of_measurement"] = self.unit_of_measurement

        return {
            "topic": _topic_url(self.device_name, self.snake_name),
            "payload": payload,
        }

    def publish(self, client):
        x = self.to_dict()
        client.publish(
            topic=x["topic"],
            payload=json.dumps(x["payload"]),
            qos=1,
            retain=True,
        )


def _topic_url(device_name, key):
    return f"homeassistant/sensor/{device_name}/{key}/config"


def _payload_name(device_display_name, suffix):
    return f"{device_display_name} {suffix}"


# Note: I'm not sure where value_json is defined, it does not seem to come from this file.
def _value_json_name(key):
    return "{{value_json." + key + "}}"


def _unique_id(device_name, key):
    return f"{device_name}_{key}"


def _state_topic(device_name, value="state"):
    return f"system-sensors/sensor/{device_name}/{value}"


def send_config_message(client):
    print_flush("send config message")
    messages = [
        Message(
            deviceName,
            deviceNameDisplay,
            deviceManufacturer,
            deviceModel,
            "temperature",
            "Temperature",
            "mdi:thermometer",
            unit_of_measurement="°C",
            device_class="temperature",
        ),
        Message(
            deviceName,
            deviceNameDisplay,
            deviceManufacturer,
            deviceModel,
            "disk_use",
            "Disk Use",
            "mdi:micro-sd",
            unit_of_measurement="%",
        ),
        Message(
            deviceName,
            deviceNameDisplay,
            deviceManufacturer,
            deviceModel,
            "memory_use",
            "Memory Use",
            "mdi:memory",
            unit_of_measurement="%",
        ),
        Message(
            deviceName,
            deviceNameDisplay,
            deviceManufacturer,
            deviceModel,
            "cpu_usage",
            "Cpu Usage",
            "mdi:memory",
            unit_of_measurement="%",
        ),
        Message(
            deviceName,
            deviceNameDisplay,
            deviceManufacturer,
            deviceModel,
            "swap_usage",
            "Swap Usage",
            "mdi:harddisk",
            unit_of_measurement="%",
        ),
        Message(
            deviceName,
            deviceNameDisplay,
            deviceManufacturer,
            deviceModel,
            "last_boot",
            "Last Boot",
            "mdi:clock",
        ),
        Message(
            deviceName,
            deviceNameDisplay,
            deviceManufacturer,
            deviceModel,
            "hostname",
            "Hostname",
            "mdi:card-account-details",
        ),
        Message(
            deviceName,
            deviceNameDisplay,
            deviceManufacturer,
            deviceModel,
            "host_ip",
            "Host Ip",
            "mdi:lan",
        ),
        Message(
            deviceName,
            deviceNameDisplay,
            deviceManufacturer,
            deviceModel,
            "host_os",
            "Host OS",
            "mdi:linux",
        ),
        Message(
            deviceName,
            deviceNameDisplay,
            deviceManufacturer,
            deviceModel,
            "host_arch",
            "Host Architecture",
            "mdi:chip",
        ),
        Message(
            deviceName,
            deviceNameDisplay,
            deviceManufacturer,
            deviceModel,
            "network_in",
            "Network In",
            "mdi:arrow-down",
            unit_of_measurement="kB/s",
        ),
        Message(
            deviceName,
            deviceNameDisplay,
            deviceManufacturer,
            deviceModel,
            "network_out",
            "Network Out",
            "mdi:arrow-up",
            unit_of_measurement="kB/s",
        ),
    ]

    if settings.get("check_available_updates"):
        if apt_enabled:
            messages.append(
                Message(
                    deviceName,
                    deviceNameDisplay,
                    deviceManufacturer,
                    deviceModel,
                    "updates",
                    "Update Available",
                    "mdi:cellphone-arrow-down",
                )
            )
        else:
            print_flush("import of apt failed!")

        if "rasp" in OS_DATA["ID"]:
                mqttClient.publish(
                    topic=f"homeassistant/switch/{deviceName}/display/config",
                    payload='{'
                            + f"\"name\":\"{deviceNameDisplay} Display Switch\","
                            + f"\"unique_id\":\"{deviceName}_switch_display\","
                            + f"\"availability_topic\":\"system-sensors/sensor/{deviceName}/availability\","
                            + f"\"command_topic\":\"system-sensors/sensor/{deviceName}/command\","
                            + f"\"state_topic\":\"system-sensors/sensor/{deviceName}/state\","
                            + '"value_template":"{{value_json.display}}",'
                            + '"state_off":"0",'
                            + '"state_on":"1",'
                            + '"payload_off":"display_off",'
                            + '"payload_on":"display_on",'
                            + f"\"device\":{{"
                                + f"\"identifiers\":[\"{deviceName}_sensor\"],"
                                + f"\"name\":\"{deviceNameDisplay} Sensors\","
                                + f"\"model\":\"{deviceModel}\","
                                + f'"manufacturer":\"{deviceManufacturer}\"'
                            + '},'
                            + '"icon":"mdi:monitor"}',
                    qos=1,
                    retain=True,
                )

    
    if settings.get("enable_rust_server"):
        rust_messages = [
            Message(
                deviceName,
                deviceNameDisplay,
                deviceManufacturer,
                deviceModel,
                "rust_server_max_players",
                "Max players",
                "mdi:cellphone-arrow-down",
                unit_of_measurement="Players",
                availability_topic="rust_server_availability",
                unique_id_prefix="",
            ),
            Message(
                deviceName,
                deviceNameDisplay,
                deviceManufacturer,
                deviceModel,
                "rust_server_players",
                "Players",
                "mdi:account-group",
                unit_of_measurement="Players",
                availability_topic="rust_server_availability",
                unique_id_prefix="",
            ),
            Message(
                deviceName,
                deviceNameDisplay,
                deviceManufacturer,
                deviceModel,
                "rust_server_players_queued",
                "Queued",
                "mdi:human-queue",
                unit_of_measurement="Players",
                availability_topic="rust_server_availability",
                unique_id_prefix="",
            ),
            Message(
                deviceName,
                deviceNameDisplay,
                deviceManufacturer,
                deviceModel,
                "rust_server_players_joining",
                "Joining",
                "mdi:account-group",
                unit_of_measurement="Players",
                availability_topic="rust_server_availability",
                unique_id_prefix="",
            ),
            Message(
                deviceName,
                deviceNameDisplay,
                deviceManufacturer,
                deviceModel,
                "rust_server_entity_count",
                "Entity Count",
                "mdi:pine-tree",
                unit_of_measurement="Entities",
                availability_topic="rust_server_availability",
                unique_id_prefix="",
            ),
            Message(
                deviceName,
                deviceNameDisplay,
                deviceManufacturer,
                deviceModel,
                "rust_server_framerate",
                "Framerate",
                "mdi:crosshairs",
                unit_of_measurement="Entities",
                availability_topic="rust_server_availability",
                unique_id_prefix="",
            ),
            Message(
                deviceName,
                deviceNameDisplay,
                deviceManufacturer,
                deviceModel,
                "rust_server_memory",
                "Rust Server Memory",
                "mdi:memory",
                unit_of_measurement="MB",
                availability_topic="rust_server_availability",
                unique_id_prefix="",
            ),
            Message(
                deviceName,
                deviceNameDisplay,
                deviceManufacturer,
                deviceModel,
                "rust_server_network_in",
                "Rust Network In",
                "mdi:arrow-down",
                unit_of_measurement="kB/s",
                availability_topic="rust_server_availability",
                unique_id_prefix="",
            ),
            Message(
                deviceName,
                deviceNameDisplay,
                deviceManufacturer,
                deviceModel,
                "rust_server_network_out",
                "Rust Network Out",
                "mdi:arrow-up",
                unit_of_measurement="kB/s",
                availability_topic="rust_server_availability",
                unique_id_prefix="",
            ),
        ]
        messages.extend(rust_messages)

    if settings.get("check_wifi_strength"):
        messages.append(
            Message(
                deviceName,
                deviceNameDisplay,
                deviceManufacturer,
                deviceModel,
                "wifi_strength",
                "Wifi Strength",
                "mdi:wifi",
                unit_of_measurement="dBm",
                device_class="signal_strength",
            )
        )

    for drive in settings.get("external_drives", []):
        messges.append(
            Message(
                deviceName,
                deviceNameDisplay,
                deviceManufacturer,
                deviceModel,
                f"disk_use_{drive.lower()}",
                f"Disk Use {drive}",
                "mdi:harddisk",
                unit_of_measurement="%",
            )
        )

    for message in messages:
        message.publish(client)

    client.publish(
        f"system-sensors/sensor/{deviceName}/availability", "online", retain=True
    )
    if settings.get("enable_rust_server"):
        client.publish(
            f"system-sensors/sensor/{deviceName}/rust_server_availability",
            "online",
            retain=True,
        )


def _parser():
    """Generate argument parser"""
    parser = argparse.ArgumentParser()
    parser.add_argument("settings", help="path to the settings file")
    return parser


def _on_connect(client, userdata, flags, rc):
    if rc == 0:
        print_flush("Connected to broker")
        client.subscribe("hass/status")
        mqttClient.publish(
            f"system-sensors/sensor/{deviceName}/availability", "online", retain=True
        )
        print("subscribing : " + f"system-sensors/sensor/{deviceName}/command")
        client.subscribe(f"system-sensors/sensor/{deviceName}/command")#subscribe
        client.publish(f"system-sensors/sensor/{deviceName}/command", "setup", retain=True)
    else:
        print_flush("Connection failed")


def _generate_mqtt_client(
    client_id,
    device_name,
    username=None,
    password=None,
    hostname="127.0.0.1",
    port=1883,
):
    client = mqtt.Client(client_id=client_id)
    client.on_connect = _on_connect  # attach function to callback
    client.on_message = _on_message
    client.will_set(
        f"system-sensors/sensor/{device_name}/availability", "offline", retain=True
    )
    if username and password:
        client.username_pw_set(username, password)
    client.connect(hostname, port)
    return client


if __name__ == "__main__":
    try:
        OS_DATA = {}
        with open("/etc/os-release") as f:
            reader = csv.reader(f, delimiter="=")
            for row in reader:
                if row:
                    OS_DATA[row[0]] = row[1]
    except:
        OS_DATA = {}

    args = _parser().parse_args()
    with open(args.settings) as f:
        settings = yaml.safe_load(f)

    check_settings(settings)
    DEFAULT_TIME_ZONE = timezone(settings["timezone"])
    WAIT_TIME_SECONDS = settings.get("update_interval", 60)

    deviceName = settings["deviceName"].replace(" ", "").lower()
    deviceNameDisplay = settings["deviceName"]
    # Complicated existence checks can be replaced with 'get' calls with fallback values. :)
    deviceManufacturer = settings.get("deviceManufacturer", settings["deviceName"])
    deviceModel = settings.get("deviceModel", settings["deviceName"])

    # using 'get' will return None if these values don't exist or are empty strings
    mqttClient = _generate_mqtt_client(
        settings["client_id"],
        deviceName,
        settings.get("mqtt", {}).get("user"),
        settings.get("mqtt", {}).get("password"),
        settings.get("mqtt", {}).get("hostname"),
        settings.get("mqtt", {}).get("port"),
    )
    mqttClient.on_message = on_message
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    remove_old_topics(
        mqttClient, deviceNameDisplay, settings.get("external_drives", [])
    )
    send_config_message(mqttClient)
    job = Job(interval=timedelta(seconds=WAIT_TIME_SECONDS), execute=update_sensors)
    job.start()

    mqttClient.loop_start()
    update_sensors()
    while True:
        try:
            sys.stdout.flush()
            time.sleep(1)
        except ProgramKilled:
            print_flush("Program killed: running cleanup code")
            mqttClient.publish(
                f"system-sensors/sensor/{deviceName}/availability",
                "offline",
                retain=True,
            )
            if settings.get("enable_rust_server"):
                mqttClient.publish(
                    f"system-sensors/sensor/{deviceName}/rust_server_availability",
                    "offline",
                    retain=True,
                )
            mqttClient.disconnect()
            mqttClient.loop_stop()
            sys.stdout.flush()
            job.stop()
            break
