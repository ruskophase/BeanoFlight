cmake_minimum_required(VERSION 3.16)

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
# USB-only actuator: include only main and its explicit dependencies.
# In particular, do not include Wi-Fi, Bluetooth, or radio PHY components.
idf_build_set_property(MINIMAL_BUILD ON)
project(beano_esp32_s2_actuator)

idf_build_get_property(actuator_components BUILD_COMPONENTS)
foreach(radio_component esp_wifi bt esp_phy esp_coex)
    if(radio_component IN_LIST actuator_components)
        message(FATAL_ERROR "USB-only actuator must not include ${radio_component}")
    endif()
endforeach()
