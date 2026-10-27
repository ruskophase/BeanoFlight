idf_component_register(SRCS "main.c" INCLUDE_DIRS "."
                       PRIV_REQUIRES esp_driver_gpio esp_driver_gptimer esp_timer
                                     esp_hw_support esp_usb_cdc_rom_console vfs)
