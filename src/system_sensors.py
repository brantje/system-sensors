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
    apt_disabled = False
except ImportError:
    apt_disabled = True
UTC = pytz.utc
DEFAULT_TIME_ZONE = None

# Get OS information
OS_DATA = {}
with open("/etc/os-release") as f:
    reader = csv.reader(f, delimiter="=")
    for row in reader:
        if row:
            OS_DATA[row[0]] = row[1]

mqttClient = None
WAIT_TIME_SECONDS = 60
deviceName = None
_underVoltage = None

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


def write_message_to_console(message):
    print(message)
    sys.stdout.flush()
    

def utc_from_timestamp(timestamp: float) -> dt.datetime:
    """Return a UTC time from a timestamp."""
    return UTC.localize(dt.datetime.utcfromtimestamp(timestamp))


def as_local(dattim: dt.datetime) -> dt.datetime:
    """Convert a UTC datetime object to local time zone."""
    if dattim.tzinfo == DEFAULT_TIME_ZONE:
        return dattim
    if dattim.tzinfo is None:
        dattim = UTC.localize(dattim)

    return dattim.astimezone(DEFAULT_TIME_ZONE)

def get_last_boot():
    return str(as_local(utc_from_timestamp(psutil.boot_time())).isoformat())

def get_last_message():
    return str(as_local(utc_from_timestamp(time.time())).isoformat())


def on_message(client, userdata, message):
    print (f"Message received: {message.payload.decode()}"  )
    if(message.payload.decode() == "online"):
        send_config_message(client)


previousResponse = False
def updateSensors():
    global previousResponse
    write_message_to_console('Updating sensors...')
    network = get_network_usage()
    payload_str = (
        '{'
        + f'"temperature": {get_temp()},'
        + f'"disk_use": {get_disk_usage("/")},'
        + f'"memory_use": {get_memory_usage()},'
        + f'"cpu_usage": {get_cpu_usage()},'
        + f'"swap_usage": {get_swap_usage()},'
        + f'"last_boot": "{get_last_boot()}",'
        + f'"last_message": "{get_last_message()}",'
        + f'"host_name": "{get_host_name()}",'
        + f'"host_ip": "{get_host_ip()}",'
        + f'"host_os": "{get_host_os()}",'
        + f'"network_out": {network["network_out_speed"]},'
        + f'"network_in": { network["network_in_speed"]},'
        + f'"host_arch": "{get_host_arch()}"'
    )


    if "check_available_updates" in settings and settings["check_available_updates"] and not apt_disabled:
        payload_str = payload_str + f', "updates": {get_updates()}' 
    
    if "enable_rust_server" in settings and settings["enable_rust_server"]:
        serverip = "localhost"
        if "rust_server_ip" in settings and settings["rust_server_ip"] != None:
            serverip = settings["rust_server_ip"]
        rcon_port = 28016
        if "rust_rcon_port" in settings and settings["rust_rcon_port"] != None:
            rcon_port = settings["rust_rcon_port"]            
        rustresponse = get_rust_server_info(serverip, rcon_port, settings["rcon_password"])
        if('MaxPlayers' not in rustresponse):
            write_message_to_console("Error fetching server info, it might be down or something else... i dunnow...")
            rustresponse = previousResponse

        payload_str = payload_str + f', "rust_server_max_players": {rustresponse["MaxPlayers"]}'        
        payload_str = payload_str + f', "rust_server_players": {rustresponse["Players"]}'        
        payload_str = payload_str + f', "rust_server_players_queued": {rustresponse["Queued"]}'        
        payload_str = payload_str + f', "rust_server_players_joining": {rustresponse["Joining"]}'        
        payload_str = payload_str + f', "rust_server_entity_count": {rustresponse["EntityCount"]}'        
        payload_str = payload_str + f', "rust_server_framerate": {rustresponse["Framerate"]}'        
        payload_str = payload_str + f', "rust_server_memory": {rustresponse["Memory"]}'        
        payload_str = payload_str + f', "rust_server_network_in": {rustresponse["NetworkIn"] / 1024}'        
        payload_str = payload_str + f', "rust_server_network_out": {rustresponse["NetworkOut"] / 1024}'        
        previousResponse = rustresponse
    
    if "check_wifi_strength" in settings and settings["check_wifi_strength"]:
        payload_str = payload_str + f', "wifi_strength": {get_wifi_strength()}'
    if "external_drives" in settings:
        for drive in settings["external_drives"]:
            payload_str = (
                payload_str + f', "disk_use_{drive.lower()}": {get_disk_usage(settings["external_drives"][drive])}'
            )
    payload_str = payload_str + "}"
    write_message_to_console(payload_str)
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
    temp = ""
    if "rasp" in OS_DATA["ID"]:
        reading = check_output(["vcgencmd", "measure_temp"]).decode("UTF-8")
        temp = str(findall("\d+\.\d+", reading)[0])
    else:
        if(path.exists("/sys/class/thermal/thermal_zone0")):
            reading = check_output(["cat", "/sys/class/thermal/thermal_zone0/temp"]).decode("UTF-8")
            temp = str(reading[0] + reading[1] + "." + reading[2])
        else:
            temp = 0.0
    return temp

def get_disk_usage(path):
    return str(psutil.disk_usage(path).percent)


def get_memory_usage():
    return str(psutil.virtual_memory().percent)


def get_cpu_usage():
    return str(psutil.cpu_percent(interval=None))


def get_swap_usage():
    return str(psutil.swap_memory().percent)


def get_network_usage():
    network_stats1 = psutil.net_io_counters()
    time.sleep(1)
    network_stats2 = psutil.net_io_counters()
    # now = time.time()
    # time_diff = now - network_stats_prev_time
    # byte_diff_rx = network_stats.get('bytes_recv') - network_stats_prev.bytes_recv
    # byte_diff_tx = network_stats.bytes_sent - network_stats.bytes_send
    tx_speed = round((network_stats2.bytes_sent - network_stats1.bytes_sent)/1024, 2)
    rx_speed = round((network_stats2.bytes_recv - network_stats1.bytes_recv)/1024, 2)
    
    result = dict()
    result['network_out_speed'] = float(tx_speed)
    result['network_in_speed'] = float(rx_speed)
    return result


def get_wifi_strength():  # check_output(["/proc/net/wireless", "grep wlan0"])
    wifi_strength_value = check_output(
                              [
                                  "bash",
                                  "-c",
                                  "cat /proc/net/wireless | grep wlan0: | awk '{print int($4)}'",
                              ]
                          ).decode("utf-8").rstrip()
    if not wifi_strength_value:
        wifi_strength_value = "0"
    return (wifi_strength_value)


def get_host_name():
    return socket.gethostname()

def get_host_ip():
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(('8.8.8.8', 80))
        return sock.getsockname()[0]
    except socket.error:
        try:
            return socket.gethostbyname(socket.gethostname())
        except socket.gaierror:
            return '127.0.0.1'
    finally:
        sock.close()

def get_host_os():
    try:     
        return OS_DATA["PRETTY_NAME"]
    except:
        return "Unknown"

def get_host_arch():    
    try:     
        return platform.machine()
    except:
        return "Unknown"

def get_rust_server_info(ip, port, password):
    server_uri = 'ws://{0}:{1}/{2}'.format(ip, port, password)
    command_json = {}
    command_json["Identifier"] = 1
    command_json["Message"] = 'serverinfo'
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
       response = "Failed to connect. {}".format(str(e))
       return response

def remove_old_topics():
    mqttClient.publish(
        topic=f"homeassistant/sensor/{deviceNameDisplay}/{deviceNameDisplay}Temp/config",
        payload='',
        qos=1,
        retain=False,
    )
    mqttClient.publish(
        topic=f"homeassistant/sensor/{deviceNameDisplay}/{deviceNameDisplay}DiskUse/config",
        payload='',
        qos=1,
        retain=False,
    )
    mqttClient.publish(
        topic=f"homeassistant/sensor/{deviceNameDisplay}/{deviceNameDisplay}MemoryUse/config",
        payload='',
        qos=1,
        retain=False,
    )
    mqttClient.publish(
        topic=f"homeassistant/sensor/{deviceNameDisplay}/{deviceNameDisplay}CpuUsage/config",
        payload='',
        qos=1,
        retain=False,
    )
    mqttClient.publish(
        topic=f"homeassistant/sensor/{deviceNameDisplay}/{deviceNameDisplay}SwapUsage/config",
        payload='',
        qos=1,
        retain=False,
    )
    mqttClient.publish(
        topic=f"homeassistant/sensor/{deviceNameDisplay}/{deviceNameDisplay}PowerStatus/config",
        payload='',
        qos=1,
        retain=False,
    )
    mqttClient.publish(
        topic=f"homeassistant/sensor/{deviceNameDisplay}/{deviceNameDisplay}LastBoot/config",
        payload='',
        qos=1,
        retain=False,
    )
    mqttClient.publish(
        topic=f"homeassistant/sensor/{deviceNameDisplay}/{deviceNameDisplay}LastMessage/config",
        payload='',
        qos=1,
        retain=False,
    )
    mqttClient.publish(
        topic=f"homeassistant/sensor/{deviceNameDisplay}/{deviceNameDisplay}WifiStrength/config",
        payload='',
        qos=1,
        retain=False,
    )
    mqttClient.publish(
        topic=f"homeassistant/sensor/{deviceNameDisplay}/{deviceNameDisplay}Updates/config",
        payload='',
        qos=1,
        retain=False,
    )

    if "external_drives" in settings:
        for drive in settings["external_drives"]:
            mqttClient.publish(
                topic=f"homeassistant/sensor/{deviceNameDisplay}/{deviceNameDisplay}DiskUse{drive}/config",
                payload='',
                qos=1,
                retain=False,
            )


def check_settings(settings):
    if "mqtt" not in settings:
        write_message_to_console("Mqtt not defined in settings.yaml! Please check the documentation")
        sys.exit()
    if "hostname" not in settings["mqtt"]:
        write_message_to_console("Hostname not defined in settings.yaml! Please check the documentation")
        sys.exit()
    if "timezone" not in settings:
        write_message_to_console("Timezone not defined in settings.yaml! Please check the documentation")
        sys.exit()
    if "deviceName" not in settings:
        write_message_to_console("deviceName not defined in settings.yaml! Please check the documentation")
        sys.exit()
    if "client_id" not in settings:
        write_message_to_console("client_id not defined in settings.yaml! Please check the documentation")
        sys.exit()
    if "power_integer_state" in settings:
        write_message_to_console("power_integer_state is deprecated please remove this option power state is now a binary_sensor!")


def send_config_message(mqttClient):
    write_message_to_console("send config message")
    mqttClient.publish(
        topic=f"homeassistant/sensor/{deviceName}/temperature/config",
        payload='{"device_class":"temperature",'
                + f"\"name\":\"{deviceNameDisplay} Temperature\","
                + f"\"state_topic\":\"system-sensors/sensor/{deviceName}/state\","
                + '"unit_of_measurement":"°C",'
                + '"value_template":"{{value_json.temperature}}",'
                + f"\"unique_id\":\"{deviceName}_sensor_temperature\","
                + f"\"availability_topic\":\"system-sensors/sensor/{deviceName}/availability\","
                + f"\"device\":{{\"identifiers\":[\"{deviceName}_sensor\"],"
                + f"\"name\":\"{deviceNameDisplay}\",\"model\":\"{deviceNameDisplay}\", \"manufacturer\":\"{deviceManufacturer}\"}},"
                + f"\"icon\":\"mdi:thermometer\"}}",
        qos=1,
        retain=True,
    )
    
    mqttClient.publish(
        topic=f"homeassistant/sensor/{deviceName}/disk_use/config",
        payload=f"{{\"name\":\"{deviceNameDisplay} Disk Use\","
                + f"\"state_topic\":\"system-sensors/sensor/{deviceName}/state\","
                + '"unit_of_measurement":"%",'
                + '"value_template":"{{value_json.disk_use}}",'
                + f"\"unique_id\":\"{deviceName}_sensor_disk_use\","
                + f"\"availability_topic\":\"system-sensors/sensor/{deviceName}/availability\","
                + f"\"device\":{{\"identifiers\":[\"{deviceName}_sensor\"],"
                + f"\"name\":\"{deviceNameDisplay}\",\"model\":\"{deviceNameDisplay}\", \"manufacturer\":\"{deviceManufacturer}\"}},"
                + f"\"icon\":\"mdi:micro-sd\"}}",
        qos=1,
        retain=True,
    )
    
    mqttClient.publish(
        topic=f"homeassistant/sensor/{deviceName}/memory_use/config",
        payload=f"{{\"name\":\"{deviceNameDisplay} Memory Use\","
                + f"\"state_topic\":\"system-sensors/sensor/{deviceName}/state\","
                + '"unit_of_measurement":"%",'
                + '"value_template":"{{value_json.memory_use}}",'
                + f"\"unique_id\":\"{deviceName}_sensor_memory_use\","
                + f"\"availability_topic\":\"system-sensors/sensor/{deviceName}/availability\","
                + f"\"device\":{{\"identifiers\":[\"{deviceName}_sensor\"],"
                + f"\"name\":\"{deviceNameDisplay}\",\"model\":\"{deviceNameDisplay}\", \"manufacturer\":\"{deviceManufacturer}\"}},"
                + f"\"icon\":\"mdi:memory\"}}",
        qos=1,
        retain=True,
    )
    
    mqttClient.publish(
        topic=f"homeassistant/sensor/{deviceName}/cpu_usage/config",
        payload=f"{{\"name\":\"{deviceNameDisplay} Cpu Usage\","
                + f"\"state_topic\":\"system-sensors/sensor/{deviceName}/state\","
                + '"unit_of_measurement":"%",'
                + '"value_template":"{{value_json.cpu_usage}}",'
                + f"\"unique_id\":\"{deviceName}_sensor_cpu_usage\","
                + f"\"availability_topic\":\"system-sensors/sensor/{deviceName}/availability\","
                + f"\"device\":{{\"identifiers\":[\"{deviceName}_sensor\"],"
                + f"\"name\":\"{deviceNameDisplay}\",\"model\":\"{deviceNameDisplay}\", \"manufacturer\":\"{deviceManufacturer}\"}},"
                + f"\"icon\":\"mdi:memory\"}}",
        qos=1,
        retain=True,
    )
    
    mqttClient.publish(
        topic=f"homeassistant/sensor/{deviceName}/swap_usage/config",
        payload=f"{{\"name\":\"{deviceNameDisplay} Swap Usage\","
                + f"\"state_topic\":\"system-sensors/sensor/{deviceName}/state\","
                + '"unit_of_measurement":"%",'
                + '"value_template":"{{value_json.swap_usage}}",'
                + f"\"unique_id\":\"{deviceName}_sensor_swap_usage\","
                + f"\"availability_topic\":\"system-sensors/sensor/{deviceName}/availability\","
                + f"\"device\":{{\"identifiers\":[\"{deviceName}_sensor\"],"
                + f"\"name\":\"{deviceNameDisplay}\",\"model\":\"{deviceNameDisplay}\", \"manufacturer\":\"{deviceManufacturer}\"}},"
                + f"\"icon\":\"mdi:harddisk\"}}",
        qos=1,
        retain=True,
    )
    
    mqttClient.publish(
        topic=f"homeassistant/sensor/{deviceName}/last_boot/config",
        payload='{"device_class":"timestamp",'
                + f"\"name\":\"{deviceNameDisplay} Last Boot\","
                + f"\"state_topic\":\"system-sensors/sensor/{deviceName}/state\","
                + '"value_template":"{{value_json.last_boot}}",'
                + f"\"unique_id\":\"{deviceName}_sensor_last_boot\","
                + f"\"availability_topic\":\"system-sensors/sensor/{deviceName}/availability\","
                + f"\"device\":{{\"identifiers\":[\"{deviceName}_sensor\"],"
                + f"\"name\":\"{deviceNameDisplay}\",\"model\":\"{deviceNameDisplay}\", \"manufacturer\":\"{deviceManufacturer}\"}},"
                + f"\"icon\":\"mdi:clock\"}}",
        qos=1,
        retain=True,
    )
    mqttClient.publish(
        topic=f"homeassistant/sensor/{deviceName}/hostname/config",
        payload=f"{{\"name\":\"{deviceNameDisplay} Hostname\","
                + f"\"state_topic\":\"system-sensors/sensor/{deviceName}/state\","
                + '"value_template":"{{value_json.host_name}}",'
                + f"\"unique_id\":\"{deviceName}_sensor_host_name\","
                + f"\"availability_topic\":\"system-sensors/sensor/{deviceName}/availability\","
                + f"\"device\":{{\"identifiers\":[\"{deviceName}_sensor\"],"
                + f"\"name\":\"{deviceNameDisplay}\",\"model\":\"{deviceNameDisplay}\", \"manufacturer\":\"{deviceManufacturer}\"}},"
                + f"\"icon\":\"mdi:card-account-details\"}}",
        qos=1,
        retain=True,
    )
    mqttClient.publish(
        topic=f"homeassistant/sensor/{deviceName}/host_ip/config",
        payload=f"{{\"name\":\"{deviceNameDisplay} Host Ip\","
                + f"\"state_topic\":\"system-sensors/sensor/{deviceName}/state\","
                + '"value_template":"{{value_json.host_ip}}",'
                + f"\"unique_id\":\"{deviceName}_sensor_host_ip\","
                + f"\"availability_topic\":\"system-sensors/sensor/{deviceName}/availability\","
                + f"\"device\":{{\"identifiers\":[\"{deviceName}_sensor\"],"
                + f"\"name\":\"{deviceNameDisplay}\",\"model\":\"{deviceNameDisplay}\", \"manufacturer\":\"{deviceManufacturer}\"}},"
                + f"\"icon\":\"mdi:lan\"}}",
        qos=1,
        retain=True,
    )
    mqttClient.publish(
        topic=f"homeassistant/sensor/{deviceName}/host_os/config",
        payload=f"{{\"name\":\"{deviceNameDisplay} Host OS\","
                + f"\"state_topic\":\"system-sensors/sensor/{deviceName}/state\","
                + '"value_template":"{{value_json.host_os}}",'
                + f"\"unique_id\":\"{deviceName}_sensor_host_os\","
                + f"\"availability_topic\":\"system-sensors/sensor/{deviceName}/availability\","
                + f"\"device\":{{\"identifiers\":[\"{deviceName}_sensor\"],"
                + f"\"name\":\"{deviceNameDisplay}\",\"model\":\"{deviceNameDisplay}\", \"manufacturer\":\"{deviceManufacturer}\"}},"
                + f"\"icon\":\"mdi:linux\"}}",
        qos=1,
        retain=True,
    )
    mqttClient.publish(
        topic=f"homeassistant/sensor/{deviceName}/host_arch/config",
        payload=f"{{\"name\":\"{deviceNameDisplay} Host Architecture\","
                + f"\"state_topic\":\"system-sensors/sensor/{deviceName}/state\","
                + '"value_template":"{{value_json.host_arch}}",'
                + f"\"unique_id\":\"{deviceName}_sensor_host_arch\","
                + f"\"availability_topic\":\"system-sensors/sensor/{deviceName}/availability\","
                + f"\"device\":{{\"identifiers\":[\"{deviceName}_sensor\"],"
                + f"\"name\":\"{deviceNameDisplay}\",\"model\":\"{deviceNameDisplay}\", \"manufacturer\":\"{deviceManufacturer}\"}},"
                + f"\"icon\":\"mdi:chip\"}}",
        qos=1,
        retain=True,
    )

    mqttClient.publish(
        topic=f"homeassistant/sensor/{deviceName}/network_in/config",
        payload=f"{{\"name\":\"{deviceNameDisplay} Network In\","
                + f"\"state_topic\":\"system-sensors/sensor/{deviceName}/state\","
                + '"unit_of_measurement":"kB/s",'
                + '"value_template":"{{value_json.network_in}}",'
                + f"\"unique_id\":\"{deviceName}_network_in\","
                + f"\"availability_topic\":\"system-sensors/sensor/{deviceName}/availability\","
                + f"\"device\":{{\"identifiers\":[\"{deviceName}_sensor\"],"
                + f"\"name\":\"{deviceNameDisplay}\",\"model\":\"{deviceModel}\", \"manufacturer\":\"{deviceManufacturer}\"}},"
                + f"\"icon\":\"mdi:arrow-down\"}}",
        qos=1,
        retain=True,
    )

    mqttClient.publish(
        topic=f"homeassistant/sensor/{deviceName}/network_out/config",
        payload=f"{{\"name\":\"{deviceNameDisplay} Network Out\","
                + f"\"state_topic\":\"system-sensors/sensor/{deviceName}/state\","
                + '"unit_of_measurement":"kB/s",'
                + '"value_template":"{{value_json.network_out}}",'
                + f"\"unique_id\":\"{deviceName}_network_out\","
                + f"\"availability_topic\":\"system-sensors/sensor/{deviceName}/availability\","
                + f"\"device\":{{\"identifiers\":[\"{deviceName}_sensor\"],"
                + f"\"name\":\"{deviceNameDisplay}\",\"model\":\"{deviceModel}\", \"manufacturer\":\"{deviceManufacturer}\"}},"
                + f"\"icon\":\"mdi:arrow-up\"}}",
        qos=1,
        retain=True,
    )

    if "check_available_updates" in settings and settings["check_available_updates"]:
        # import apt
        if(apt_disabled):
            write_message_to_console("import of apt failed!")
        else:
            mqttClient.publish(
                topic=f"homeassistant/sensor/{deviceName}/updates/config",
                payload=f"{{\"name\":\"{deviceNameDisplay} Update Available\","
                        + f"\"state_topic\":\"system-sensors/sensor/{deviceName}/state\","
                        + '"value_template":"{{value_json.updates}}",'
                        + f"\"unique_id\":\"{deviceName}_sensor_updates\","
                        + f"\"availability_topic\":\"system-sensors/sensor/{deviceName}/availability\","
                        + f"\"device\":{{\"identifiers\":[\"{deviceName}_sensor\"],"
                        + f"\"name\":\"{deviceNameDisplay}\",\"model\":\"{deviceModel}\", \"manufacturer\":\"{deviceManufacturer}\"}},"
                        + f"\"icon\":\"mdi:cellphone-arrow-down\"}}",
                qos=1,
                retain=True,
            )
            

    if "enable_rust_server" in settings and settings["enable_rust_server"]:
        mqttClient.publish(
            topic=f"homeassistant/sensor/{deviceName}/rustserver_maxplayers/config",
            payload=f"{{\"name\":\"{deviceNameDisplay} Max players\","
                    + f"\"state_topic\":\"system-sensors/sensor/{deviceName}/state\","
                    + '"unit_of_measurement":"Players",'
                    + '"value_template":"{{value_json.rust_server_max_players}}",'
                    + f"\"unique_id\":\"{deviceName}_rustserver_max_players\","
                    + f"\"availability_topic\":\"system-sensors/sensor/{deviceName}/availability\","
                    + f"\"device\":{{\"identifiers\":[\"{deviceName}_sensor\"],"
                    + f"\"name\":\"{deviceNameDisplay}\",\"model\":\"{deviceModel}\", \"manufacturer\":\"{deviceManufacturer}\"}},"
                    + f"\"icon\":\"mdi:account-group\"}}",
            qos=1,
            retain=True,
        )
  
        mqttClient.publish(
            topic=f"homeassistant/sensor/{deviceName}/rustserver_players/config",
            payload=f"{{\"name\":\"{deviceNameDisplay} Players\","
                    + f"\"state_topic\":\"system-sensors/sensor/{deviceName}/state\","
                    + '"unit_of_measurement":"Players",'
                    + '"value_template":"{{value_json.rust_server_players}}",'
                    + f"\"unique_id\":\"{deviceName}_rustserver_players\","
                    + f"\"availability_topic\":\"system-sensors/sensor/{deviceName}/availability\","
                    + f"\"device\":{{\"identifiers\":[\"{deviceName}_sensor\"],"
                    + f"\"name\":\"{deviceNameDisplay}\",\"model\":\"{deviceModel}\", \"manufacturer\":\"{deviceManufacturer}\"}},"
                    + f"\"icon\":\"mdi:account-group\"}}",
            qos=1,
            retain=True,
        )
            
        mqttClient.publish(
            topic=f"homeassistant/sensor/{deviceName}/rustserver_players_queued/config",
            payload=f"{{\"name\":\"{deviceNameDisplay} Queued\","
                    + f"\"state_topic\":\"system-sensors/sensor/{deviceName}/state\","
                    + '"unit_of_measurement":"Players",'
                    + '"value_template":"{{value_json.rust_server_players_queued}}",'
                    + f"\"unique_id\":\"{deviceName}_rustserver_players_queued\","
                    + f"\"availability_topic\":\"system-sensors/sensor/{deviceName}/availability\","
                    + f"\"device\":{{\"identifiers\":[\"{deviceName}_sensor\"],"
                    + f"\"name\":\"{deviceNameDisplay}\",\"model\":\"{deviceModel}\", \"manufacturer\":\"{deviceManufacturer}\"}},"
                    + f"\"icon\":\"mdi:human-queue\"}}",
            qos=1,
            retain=True,
        )
            
        mqttClient.publish(
            topic=f"homeassistant/sensor/{deviceName}/rustserver_players_joining/config",
            payload=f"{{\"name\":\"{deviceNameDisplay} Joining\","
                    + f"\"state_topic\":\"system-sensors/sensor/{deviceName}/state\","
                    + '"unit_of_measurement":"Players",'
                    + '"value_template":"{{value_json.rust_server_players_joining}}",'
                    + f"\"unique_id\":\"{deviceName}_rustserver_players_joining\","
                    + f"\"availability_topic\":\"system-sensors/sensor/{deviceName}/availability\","
                    + f"\"device\":{{\"identifiers\":[\"{deviceName}_sensor\"],"
                    + f"\"name\":\"{deviceNameDisplay}\",\"model\":\"{deviceModel}\", \"manufacturer\":\"{deviceManufacturer}\"}},"
                    + f"\"icon\":\"mdi:account-group\"}}",
            qos=1,
            retain=True,
        )

        mqttClient.publish(
            topic=f"homeassistant/sensor/{deviceName}/rustserver_entity_count/config",
            payload=f"{{\"name\":\"{deviceNameDisplay} Entity Count\","
                    + f"\"state_topic\":\"system-sensors/sensor/{deviceName}/state\","
                    + '"unit_of_measurement":"Entities",'
                    + '"value_template":"{{value_json.rust_server_entity_count}}",'
                    + f"\"unique_id\":\"{deviceName}_rustserver_entity_count\","
                    + f"\"availability_topic\":\"system-sensors/sensor/{deviceName}/availability\","
                    + f"\"device\":{{\"identifiers\":[\"{deviceName}_sensor\"],"
                    + f"\"name\":\"{deviceNameDisplay}\",\"model\":\"{deviceModel}\", \"manufacturer\":\"{deviceManufacturer}\"}},"
                    + f"\"icon\":\"mdi:pine-tree\"}}",
            qos=1,
            retain=True,
        )

        mqttClient.publish(
            topic=f"homeassistant/sensor/{deviceName}/rustserver_framerate/config",
            payload=f"{{\"name\":\"{deviceNameDisplay} Framerate\","
                    + f"\"state_topic\":\"system-sensors/sensor/{deviceName}/state\","
                    + '"unit_of_measurement":"FPS",'
                    + '"value_template":"{{value_json.rust_server_framerate}}",'
                    + f"\"unique_id\":\"{deviceName}_rustserver_framerate\","
                    + f"\"availability_topic\":\"system-sensors/sensor/{deviceName}/availability\","
                    + f"\"device\":{{\"identifiers\":[\"{deviceName}_sensor\"],"
                    + f"\"name\":\"{deviceNameDisplay}\",\"model\":\"{deviceModel}\", \"manufacturer\":\"{deviceManufacturer}\"}},"
                    + f"\"icon\":\"mdi:crosshairs\"}}",
            qos=1,
            retain=True,
        )                      

        mqttClient.publish(
            topic=f"homeassistant/sensor/{deviceName}/rustserver_memory/config",
            payload=f"{{\"name\":\"{deviceNameDisplay} Rust Server Memory\","
                    + f"\"state_topic\":\"system-sensors/sensor/{deviceName}/state\","
                    + '"unit_of_measurement":"MB",'
                    + '"value_template":"{{value_json.rust_server_memory}}",'
                    + f"\"unique_id\":\"{deviceName}_rustserver_memory\","
                    + f"\"availability_topic\":\"system-sensors/sensor/{deviceName}/availability\","
                    + f"\"device\":{{\"identifiers\":[\"{deviceName}_sensor\"],"
                    + f"\"name\":\"{deviceNameDisplay}\",\"model\":\"{deviceModel}\", \"manufacturer\":\"{deviceManufacturer}\"}},"
                    + f"\"icon\":\"mdi:memory\"}}",
            qos=1,
            retain=True,
        )

        mqttClient.publish(
            topic=f"homeassistant/sensor/{deviceName}/rustserver_network_in/config",
            payload=f"{{\"name\":\"{deviceNameDisplay} Rust Network In\","
                    + f"\"state_topic\":\"system-sensors/sensor/{deviceName}/state\","
                    + '"unit_of_measurement":"kB/s",'
                    + '"value_template":"{{value_json.rust_server_network_in}}",'
                    + f"\"unique_id\":\"{deviceName}_rustserver_network_in\","
                    + f"\"availability_topic\":\"system-sensors/sensor/{deviceName}/availability\","
                    + f"\"device\":{{\"identifiers\":[\"{deviceName}_sensor\"],"
                    + f"\"name\":\"{deviceNameDisplay}\",\"model\":\"{deviceModel}\", \"manufacturer\":\"{deviceManufacturer}\"}},"
                    + f"\"icon\":\"mdi:arrow-down\"}}",
            qos=1,
            retain=True,
        )

        mqttClient.publish(
            topic=f"homeassistant/sensor/{deviceName}/rustserver_network_out/config",
            payload=f"{{\"name\":\"{deviceNameDisplay} Rust Network Out\","
                    + f"\"state_topic\":\"system-sensors/sensor/{deviceName}/state\","
                    + '"unit_of_measurement":"kB/s",'
                    + '"value_template":"{{value_json.rust_server_network_out}}",'
                    + f"\"unique_id\":\"{deviceName}_sensor_rustserver_network_out\","
                    + f"\"availability_topic\":\"system-sensors/sensor/{deviceName}/availability\","
                    + f"\"device\":{{\"identifiers\":[\"{deviceName}_sensor\"],"
                    + f"\"name\":\"{deviceNameDisplay}\",\"model\":\"{deviceModel}\", \"manufacturer\":\"{deviceManufacturer}\"}},"
                    + f"\"icon\":\"mdi:arrow-up\"}}",
            qos=1,
            retain=True,
        )


    if "check_wifi_strength" in settings and settings["check_wifi_strength"]:
        mqttClient.publish(
            topic=f"homeassistant/sensor/{deviceName}/wifi_strength/config",
            payload='{"device_class":"signal_strength",'
                    + f"\"name\":\"{deviceNameDisplay} Wifi Strength\","
                    + f"\"state_topic\":\"system-sensors/sensor/{deviceName}/state\","
                    + '"unit_of_measurement":"dBm",'
                    + '"value_template":"{{value_json.wifi_strength}}",'
                    + f"\"unique_id\":\"{deviceName}_sensor_wifi_strength\","
                    + f"\"availability_topic\":\"system-sensors/sensor/{deviceName}/availability\","
                    + f"\"device\":{{\"identifiers\":[\"{deviceName}_sensor\"],"
                    + f"\"name\":\"{deviceNameDisplay}\",\"model\":\"{deviceModel}\", \"manufacturer\":\"{deviceManufacturer}\"}},"
                    + f"\"icon\":\"mdi:wifi\"}}",
            qos=1,
            retain=True,
        )
        
    if "external_drives" in settings:
        for drive in settings["external_drives"]:
            mqttClient.publish(
                topic=f"homeassistant/sensor/{deviceName}/disk_use_{drive.lower()}/config",
                payload=f"{{\"name\":\"{deviceNameDisplay} Disk Use {drive}\","
                        + f"\"state_topic\":\"system-sensors/sensor/{deviceName}/state\","
                        + '"unit_of_measurement":"%",'
                        + f"\"value_template\":\"{{{{value_json.disk_use_{drive.lower()}}}}}\","
                        + f"\"unique_id\":\"{deviceName}_sensor_disk_use_{drive.lower()}\","
                        + f"\"availability_topic\":\"system-sensors/sensor/{deviceName}/availability\","
                        + f"\"device\":{{\"identifiers\":[\"{deviceName}_sensor\"],"
                        + f"\"name\":\"{deviceNameDisplay}\",\"model\":\"{deviceModel}\", \"manufacturer\":\"{deviceManufacturer}\"}},"
                        + f"\"icon\":\"mdi:harddisk\"}}",
                qos=1,
                retain=True,
            )
            

    mqttClient.publish(f"system-sensors/sensor/{deviceName}/availability", "online", retain=True)


def _parser():
    """Generate argument parser"""
    parser = argparse.ArgumentParser()
    parser.add_argument("settings", help="path to the settings file")
    return parser


def on_connect(client, userdata, flags, rc):
    if rc == 0:
        write_message_to_console("Connected to broker")
        client.subscribe("hass/status")
        mqttClient.publish(f"system-sensors/sensor/{deviceName}/availability", "online", retain=True)
    else:
        write_message_to_console("Connection failed")


if __name__ == "__main__":
    args = _parser().parse_args()
    with open(args.settings) as f:
        # use safe_load instead load
        settings = yaml.safe_load(f)
    check_settings(settings)
    DEFAULT_TIME_ZONE = timezone(settings["timezone"])
    if "update_interval" in settings:
        WAIT_TIME_SECONDS = settings["update_interval"]
    mqttClient = mqtt.Client(client_id=settings["client_id"])
    mqttClient.on_connect = on_connect                      #attach function to callback
    mqttClient.on_message = on_message
    deviceName = settings["deviceName"].replace(" ", "").lower()
    deviceNameDisplay = settings["deviceName"]
    deviceManufacturer = settings["deviceName"]
    network_stats_prev = psutil.net_io_counters()
    network_stats_prev_time = time.time();
    if "deviceManufacturer" in settings and settings["deviceManufacturer"] != "":
        deviceManufacturer = settings["deviceManufacturer"]

    deviceModel = settings["deviceName"]
    if "deviceModel" in settings and settings["deviceModel"] != "":
        deviceModel = settings["deviceModel"]
    
    mqttClient.will_set(f"system-sensors/sensor/{deviceName}/availability", "offline", retain=True)
    if "user" in settings["mqtt"]:
        mqttClient.username_pw_set(
            settings["mqtt"]["user"], settings["mqtt"]["password"]
        )  # Username and pass if configured otherwise you should comment out this
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    if "port" in settings["mqtt"]:
        mqttClient.connect(settings["mqtt"]["hostname"], settings["mqtt"]["port"])
    else:
        mqttClient.connect(settings["mqtt"]["hostname"], 1883)
    # try:
    #     # remove_old_topics()
    #     # send_config_message(mqttClient)
    # except:
    #     write_message_to_console("something whent wrong")
    
    remove_old_topics()
    send_config_message(mqttClient)
    job = Job(interval=timedelta(seconds=WAIT_TIME_SECONDS), execute=updateSensors)
    job.start()

    mqttClient.loop_start()

    while True:
        try:
            sys.stdout.flush()
            time.sleep(1)
        except ProgramKilled:
            write_message_to_console("Program killed: running cleanup code")
            mqttClient.publish(f"system-sensors/sensor/{deviceName}/availability", "offline", retain=True)
            mqttClient.disconnect()
            mqttClient.loop_stop()
            sys.stdout.flush()
            job.stop()
            break
