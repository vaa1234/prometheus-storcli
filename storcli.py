"""
Script to parse StorCLI's JSON output and expose MegaRAID metrics via Prometheus client.

Tested against StorCLI '007.1616.0000.0000 Dec 24, 2020'.

StorCLI reference manual:
https://docs.broadcom.com/doc/12352476

JSON key abbreviations used by StorCLI are documented in the standard command
output, i.e.  when you omit the trailing 'J' from the command.
"""

from __future__ import print_function
from datetime import datetime
import argparse
import collections
import json
import os
import shlex
import subprocess
import prometheus_client
import time

DESCRIPTION = """Parses StorCLI's JSON output and exposes MegaRAID health as metrics via prometheus client."""
VERSION = '0.1.0'

METRICS = {}
metric_prefix = 'megaraid_'
exporter_address = os.environ.get("STORCLI_EXPORTER_ADDRESS", "0.0.0.0")
exporter_port = int(os.environ.get("STORCLI_EXPORTER_PORT", 9909))
refresh_interval = int(os.environ.get("STORCLI_REFRESH_INTERVAL", 60))
storcli_path = os.environ.get("STORCLI_PATH", "/opt/hpe/storcli/storcli64")

def main(args):
    # Start Prometheus server
    prometheus_client.start_http_server(exporter_port, exporter_address)
    print(f"Server listening in http://{exporter_address}:{exporter_port}/metrics")

    while True:
        collect()
        time.sleep(refresh_interval)


def collect():
    
    data = get_storcli_json('/cALL show all J')

    try:
        # All the information is collected underneath the Controllers key
        data = data['Controllers']

        for controller in data:
            response = controller['Response Data']

            handle_common_controller(response)
            if response['Version']['Driver Name'] == 'megaraid_sas':
                handle_megaraid_controller(response)
            elif response['Version']['Driver Name'] == 'mpt3sas':
                handle_sas_controller(response)
    except KeyError:
        pass
    
def handle_common_controller(response):

    labels = {
        'controller': response['Basics']['Controller'],
        'model': str(response['Basics']['Model']).strip(),
        'serial': str(response['Basics']['Serial Number']).strip(),
        'fwversion': str(response['Version']['Firmware Version']).strip()
    }
    add_metric('controller_info', labels, 1)

    if 'ROC temperature(Degree Celc' + 'ius)' in response['HwCfg'].keys():
        response['HwCfg']['ROC temperature(Degree Celsius)'] = response['HwCfg'].pop(
            'ROC temperature(Degree Celc' + 'ius)'
        )
        add_metric('controller_temperature', \
            {'controller': response['Basics']['Controller']}, \
                int(response['HwCfg']['ROC temperature(Degree Celsius)']))


def handle_sas_controller(response):
    (controller_index, baselabel) = get_basic_controller_info(response)

    add_metric('healthy', baselabel, int(response['Status']['Controller Status'] == 'OK'))
    add_metric('ports', baselabel, response['HwCfg']['Backend Port Count'])
    try:
        # The number of physical disks is half of the number of items in this dict
        # Every disk is listed twice - once for basic info, again for detailed info
        add_metric('physical_drives', baselabel,
                    len(response['Physical Device Information'].keys()) / 2)
    except AttributeError:
        pass

    for key, basic_disk_info in response['Physical Device Information'].items():
        if 'Detailed Information' in key:
            continue
        create_metrics_of_physical_drive(basic_disk_info[0], response['Physical Device Information'], controller_index)


def handle_megaraid_controller(response):
    (controller_index, baselabel) = get_basic_controller_info(response)

    baselabel = {'controller': response['Basics']['Controller']}

    # BBU Status Optimal value is 0 for cachevault and 32 for BBU
    add_metric('battery_backup_healthy', baselabel, int(response['Status']['BBU Status'] in [0, 32]))
    add_metric('degraded', baselabel, int(response['Status']['Controller Status'] == 'Degraded'))
    add_metric('failed', baselabel, int(response['Status']['Controller Status'] == 'Failed'))
    add_metric('healthy', baselabel, int(response['Status']['Controller Status'] == 'Optimal'))
    add_metric('ports', baselabel, response['HwCfg']['Backend Port Count'])
    add_metric('scheduled_patrol_read', baselabel, int('hrs' in response['Scheduled Tasks']['Patrol Read Reoccurrence']))
    for cvidx, cvinfo in enumerate(response.get('Cachevault_Info', [])):

        labels = {'controller': response['Basics']['Controller'], 'cvidx': cvidx}

        add_metric('cv_temperature', labels, int(cvinfo['Temp'].replace('C', '')))
    
    time_difference_seconds = -1
    system_time = datetime.strptime(response['Basics'].get('Current System Date/time'),
                                    "%m/%d/%Y, %H:%M:%S")
    controller_time = datetime.strptime(response['Basics'].get('Current Controller Date/Time'),
                                        "%m/%d/%Y, %H:%M:%S")
    if system_time and controller_time:
        time_difference_seconds = abs(system_time - controller_time).seconds
        add_metric('time_difference', baselabel, time_difference_seconds)

    # Make sure it doesn't crash if it's a JBOD setup
    if 'Drive Groups' in response.keys():
        add_metric('drive_groups', baselabel, response['Drive Groups'])
        add_metric('virtual_drives', baselabel, response['Virtual Drives'])

        for virtual_drive in response['VD LIST']:
            vd_position = virtual_drive.get('DG/VD')
            drive_group, volume_group = -1, -1
            if vd_position:
                drive_group = vd_position.split('/')[0]
                volume_group = vd_position.split('/')[1]
            
            labels = {
                'controller': response['Basics']['Controller'],
                'DG': drive_group,
                'VG': volume_group,
                'name': str(virtual_drive.get('Name')).strip(),
                'cache': str(virtual_drive.get('Cache')).strip(),
                'type': str(virtual_drive.get('TYPE')).strip(),
                'state': str(virtual_drive.get('State')).strip()
            }
            add_metric('vd_info', labels, 1)

    add_metric('physical_drives', baselabel, response['Physical Drives'])
    
    if response['Physical Drives'] > 0:
        data = get_storcli_json('/cALL/eALL/sALL show all J')
        drive_info = data['Controllers'][controller_index]['Response Data']
    for physical_drive in response['PD LIST']:
        create_metrics_of_physical_drive(physical_drive, drive_info, controller_index)


def get_basic_controller_info(response):
    controller_index = response['Basics']['Controller']
    baselabel = 'controller="{0}"'.format(controller_index)
    return (controller_index, baselabel)


def create_metrics_of_physical_drive(physical_drive, detailed_info_array, controller_index):
    enclosure = physical_drive.get('EID:Slt').split(':')[0]
    slot = physical_drive.get('EID:Slt').split(':')[1]

    pd_baselabel = {'controller': controller_index, 'enclosure': enclosure, 'slot': slot}
    pd_info_label = pd_baselabel | {'disk_id': str(physical_drive.get('DID')).strip(),
                                    'interface': str(physical_drive.get('Intf')).strip(),
                                    'media': str(physical_drive.get('Med')).strip(),
                                    'model': str(physical_drive.get('Model')).strip(),
                                    'DG': str(physical_drive.get('DG')).strip(),
                                    'state': str(physical_drive.get('State')).strip()
                                    }

    drive_identifier = 'Drive /c' + str(controller_index) + '/e' + str(enclosure) + '/s' + str(
        slot)
    if enclosure == ' ':
        drive_identifier = 'Drive /c' + str(controller_index) + '/s' + str(slot)
    try:
        info = detailed_info_array[drive_identifier + ' - Detailed Information']
        state = info[drive_identifier + ' State']
        attributes = info[drive_identifier + ' Device attributes']
        settings = info[drive_identifier + ' Policies/Settings']

        add_metric('pd_shield_counter', pd_baselabel, state['Shield Counter'])
        add_metric('pd_media_errors', pd_baselabel, state['Media Error Count'])
        add_metric('pd_other_errors', pd_baselabel, state['Other Error Count'])
        add_metric('pd_predictive_errors', pd_baselabel, state['Predictive Failure Count'])
        add_metric('pd_smart_alerted', pd_baselabel,
                    int(state['S.M.A.R.T alert flagged by drive'] == 'Yes'))
        add_metric('pd_link_speed_gbps', pd_baselabel, attributes['Link Speed'].split('.')[0])
        add_metric('pd_device_speed_gbps', pd_baselabel, attributes['Device Speed'].split('.')[0])
        add_metric('pd_commissioned_spare', pd_baselabel,
                    int(settings['Commissioned Spare'] == 'Yes'))
        add_metric('pd_emergency_spare', pd_baselabel, int(settings['Emergency Spare'] == 'Yes'))
        pd_info_label['firmware'] = attributes['Firmware Revision'].strip()
        if 'SN' in attributes:
            pd_info_label['serial'] = attributes['SN'].strip()
    except KeyError:
        pass
    add_metric('pd_info', pd_info_label, 1)


def add_metric(name, labels, value):
    '''
    Export metric to prometheus client or generate exception

        Parameters:
            name (str): metric name
            labels (dict): metric labels, keys are label names, values are label values
            value (int or str): metric value
    '''

    try:
        # Metric name in lower case
        metric = metric_prefix + name.replace('-', '_').replace(' ', '_').replace('.', '').replace('/', '_').lower()
        label_names = list(labels.keys())
        label_values = labels.values()

        # Create metric if it does not exist
        if metric not in METRICS:
            desc = name.replace('_', ' ')
            METRICS[metric] = prometheus_client.Gauge(metric, f'({value}) {desc}', label_names)

        # Update metric
        METRICS[metric].labels(*label_values).set(value)

    except Exception as e:
        print('Exception:', e)
        pass    


def get_storcli_json(storcli_args):
    """Get storcli output in JSON format."""
    # Check if storcli is installed and executable
    if not (os.path.isfile(storcli_path) and os.access(storcli_path, os.X_OK)):
        SystemExit(1)
    storcli_cmd = shlex.split(storcli_path + ' ' + storcli_args)
    proc = subprocess.Popen(
        storcli_cmd, shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    output_json = proc.communicate()[0]
    data = json.loads(output_json.decode("utf-8"))

    if data["Controllers"][0]["Command Status"]["Status"] != "Success":
        SystemExit(1)
    return data


if __name__ == "__main__":
    PARSER = argparse.ArgumentParser(
        description=DESCRIPTION, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    PARSER.add_argument('--version', action='version', version='%(prog)s {0}'.format(VERSION))
    ARGS = PARSER.parse_args()

    main(ARGS)