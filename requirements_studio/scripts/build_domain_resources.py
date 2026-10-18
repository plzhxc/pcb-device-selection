"""Reproducible, reviewable module-to-device retrieval map (never a suitability proof)."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT.parent))
from requirements_studio.backend.module_catalog import CATALOG
from requirements_studio.backend.knowledge import catalog
from requirements_studio.backend.handoff import RequirementSpec
from requirements_studio.backend.engineering_checks import ALIASES

# Parent categories are functional circuits; these explicit leaf/family selectors
# only bound retrieval. Descendants retain their normative functional conditions.
SELECTORS={
 'P_1_1':['step_down_dc_dc_converter','step_up_buck_boost_converter','linear_regulator_ldo','charge_pump','dc_dc_pwm_controller','ac_dc_controller_and_voltage_regulator','dc_dc_power_module','nonisolated_dc_dc_power_module','professional_power_management_pmic'],
 'P_1_2':['power_sequence_control','professional_power_management_pmic'],
 'P_1_3':['monitoring_and_reset_chip','electric_energy_metering_chip','current_sensing_amplifier'],
 'P_1_4':['isolated_dc_dc_converter','isolated_power_supply_module','ac_dc_power_module','transformer_driver'],
 'P_1_5':['voltage_reference_chip'], 'P_1_6':['current_source_constant_current_source','lighting_driver'],
 'P_1_7':['fast_charging_protocol_chip','usb_type_c_port_controller','power_over_ethernet_poe_controller'],
 'P_1_8':['battery_management','wireless_charging_and_discharging_chip','fast_charging_protocol_chip'],
 'P_1_9':['power_electronic_switch','field_effect_transistor_mosfet','ideal_diode_oring_controller'],
 'P_1_10':['ideal_diode_oring_controller','battery_management','power_electronic_switch','dc_ac_module_inverter'],
 'P_2_1':['clock_buffer_driver_distributor','signal_buffer_repeater_distributor'],
 'P_2_2':['clock_generator_frequency_synthesizer_pll','network_clock_synchronization_synce_ieee_1588'],
 'P_2_3':['@F06','@F27'], 'P_2_4':['converter_level_shifter','lvds_chip','clock_buffer_driver_distributor'],
 'P_3_1':['@F04'], 'P_3_2':['active_filter','switched_capacitor_filter','operational_amplifier','precision_operational_amplifier'],
 'P_3_3':['converter_level_shifter','buffer_driver_transceiver','digital_isolator'],
 'P_3_4':['analog_switch_multiplexer','analog_switch_special_purpose','signal_switch_codec_multiplexer'],
 'P_3_5':['comparator','isolated_comparator','pin_electronics_driver_comparator'],
 'P_3_6':['analog_to_digital_conversion_chip_adc','digital_to_analog_converter_chip_dac','analog_front_end_afe','data_acquisition_system','isolated_adc'],
 'P_3_7':['v_f_and_f_v_conversion_chip'],
 'P_4_1':['bus_to_uart_converter','protocol_bridge','usb_conversion_chip','serializer_deserializer','rs232_chip','rs_485_rs_422_chip'],
 'P_4_2':['@F30','usb_type_c_port_controller','telecommunication_interface_ic','usb_conversion_chip','usb_hub'],
 'P_4_3':['can_controller','can_transceiver','isolated_can_transceiver','isolated_can_transceiver_with_power_supply','can_bus_module'],
 'P_4_4':['ethernet_controller','ethernet_transceiver','ethernet_switch','ethernet_module','network_port_transformer'],
 'P_4_5':['i2c_bus_switch','signal_switch_codec_multiplexer','sdio_port_expander','analog_switch_multiplexer'],
 'P_5_1':['eeprom','nor_flash','nand_flash','non_volatile_memory_rom','ferroelectric_random_access_memory_fram','nvsram_non_volatile_sram'],
 'P_5_2':['sram_memory','psram_pseudo_static_random_access_memory','dram_memory','ddr_sdram','synchronous_dynamic_random_access_memory_sdram','fifo_memory','random_access_memory_ram'],
 'P_5_3':['emmc'], 'P_6_1':['@F26','electrostatic_and_surge_protection_tvs_esd','zener_diode'],
 'P_6_2':['power_electronic_switch','surge_protector','ntc_thermistor','professional_power_management_pmic'],
 'P_6_3':['ideal_diode_oring_controller','field_effect_transistor_mosfet','rectifier_bridge'],
 'P_7_1':['temperature_sensor','temperature_and_humidity_sensor','ntc_thermistor','ptc_thermistor'],
 'P_7_2':['current_sensor','current_sensing_amplifier','electric_energy_metering_chip','sensor_measurement_converter'],
 'P_7_3':['ambient_light_sensor','photodiode','phototransistor','photosensitive_resistor','image_sensor','fiber_optic_laser_sensor'],
 'P_7_4':['attitude_sensor_gyroscope','tilt_sensor'],
 'P_7_5':['magnetic_angle_sensor','rotary_encoder','resolver_to_digital_converter','position_sensor'],
 'P_7_6':['@F18'], 'P_7_7':['vibration_sensor','attitude_sensor_gyroscope'],
 'P_7_8':['pressure_sensor','force_sensor_and_load_cell'],
 'P_7_9':['capacitive_sensor_ic','capacitive_touch_sensor_ic','inductive_capacitive_sensing_interface'],
 'P_7_10':['inductive_capacitive_sensing_interface','sensor_measurement_converter'],
 'P_7_11':['gas_sensor','voc_sensor','gas_flow_sensor'], 'P_7_12':['ultrasonic_transceiver','sensor_module'],
 'P_8_1':['led_digital_display','led_dot_matrix_screen','digital_tube_driver','led_driver'],
 'P_8_2':['lcd_display_screen','oled_display_screen','lcd_driver','oled_driver'],
 'P_8_3':['buzzer','loudspeaker_speaker','audio_power_amplifier','audio_codec'],
 'P_8_4':['mems_microphone','microphone_head_microphone','audio_codec','audio_interface_chip'],
 'P_8_5':['microcontroller_mcu_mpu_soc','digital_signal_processor_dsp_dsc','audio_and_video_module'],
 'P_8_6':['@F31','capacitive_touch_sensor_ic','touch_screen_controller','rotary_encoder'],
 'P_8_7':['video_interface_chip','serializer_deserializer','audio_and_video_module','video_amplifier'],
 'P_9_1':['antenna','rf_coaxial_connector','rf_connecting_cable','rf_transformer','balun'],
 'P_9_2':['@F14'],
 'P_9_3':['wireless_transceiver_chip','lora_module','wifi_module','bluetooth_module','zigbee_module','cellular_2g_3g_4g_5g_module','radio_frequency_module','rfid_module','radio_frequency_card_chip'],
 'P_9_4':['satellite_positioning_module'], 'P_10_1':['@F11','@F12'],
 'P_10_2':['@F01'],
 'P_10_3':['secure_element','secure_authenticator','cryptographic_coprocessor','trusted_platform_module','secure_memory'],
 'P_10_4':['@F09','field_effect_transistor_mosfet','gate_driver_chip','relay_coil_driver_chip'],
 'P_10_5':['@F02','i_o_expander'],
}


def build(refresh=False):
 resource=ROOT/'backend/resources';snapshot=resource/'device_taxonomy_snapshot.json'
 if refresh or not snapshot.exists():
  taxonomy=catalog.taxonomy();snapshot.write_text(json.dumps(taxonomy,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
 else:taxonomy=json.loads(snapshot.read_text(encoding='utf-8'))
 nodes=taxonomy['nodes'];byid={n['category_id']:n for n in nodes}
 leaves={n['category_code']:n for n in nodes if n['node_type']=='LEAF'}
 def expand(selectors):
  result=[]
  for selector in selectors:
   if selector.startswith('@'):
    family=selector[1:];result.extend(n['category_code'] for n in leaves.values() if byid.get(n['parent_id'],{}).get('category_code')==family)
   else:
    key='l_'+selector
    if key not in leaves:raise ValueError('映射的Leaf不属于本版taxonomy：'+key)
    result.append(key)
  return sorted(set(result))
 assert set(SELECTORS)=={p['id'] for p in CATALOG['parents']}
 mappings={}
 for p in CATALOG['parents']:
  codes=expand(SELECTORS[p['id']]);mappings[p['id']]={'type_name':p['name'],'status':'mapped' if len(codes)==1 else 'multi_leaf',
   'taxonomy_version_id':taxonomy['version'],'leaf_codes':codes,'selectors':SELECTORS[p['id']],
   'reason':'按规范功能选择的检索范围；候选仍需核对用途、参数、对象层级和实际电路边界。'}
 for child in CATALOG['modules']:
  parent=mappings[child['parent_id']]
  mappings[child['id']]={'type_name':child['name'],'status':'inherited','parent_type_id':child['parent_id'],
   'taxonomy_version_id':taxonomy['version'],'leaf_codes':parent['leaf_codes'],
   'reason':'采用父类别检索范围；具体子类由原规范功能与参数进一步核对，不以目录类别宣称满足。'}
 result={'schema_version':'requirements-studio/module-device-map/1','module_source_sha256':CATALOG['source_sha256'],
  'taxonomy_version_id':taxonomy['version'],'purpose':'bounded_retrieval_not_suitability','mappings':mappings}
 (resource/'module_device_map.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
 (resource/'device_field_rules.json').write_text(json.dumps({'version':1,'aliases':ALIASES,'policy':'same object, dimension, rating and conditions; missing remains unknown'},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
 schemas=ROOT/'schemas';schemas.mkdir(exist_ok=True)
 (schemas/'requirement-spec.schema.json').write_text(json.dumps(RequirementSpec.model_json_schema(),ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
 return {'module_types':len(mappings),'parents':len(SELECTORS),'children':len(CATALOG['modules']),
         'taxonomy_version':taxonomy['version'],'mapped_leaf_count':len({c for m in mappings.values() for c in m['leaf_codes']})}


if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--refresh',action='store_true');args=p.parse_args()
 print(json.dumps(build(args.refresh),ensure_ascii=False,indent=2))
