#!/usr/bin/env python3
"""
Script de debug para descubrir el protocolo del BMS
Lee todas las características y prueba escribir en todas
"""

import asyncio
from bleak import BleakClient, BleakScanner


async def main():
    # Escanear
    print("Escaneando dispositivos BLE (10s)...")
    devices = await BleakScanner.discover(timeout=10.0, return_adv=True)
    
    device_list = []
    for addr, (device, adv) in devices.items():
        name = device.name or adv.local_name or ""
        rssi = adv.rssi if hasattr(adv, 'rssi') else -100
        device_list.append((addr, name, rssi))
    
    device_list.sort(key=lambda x: -x[2])
    
    print(f"\nDispositivos encontrados ({len(device_list)}):")
    for i, (addr, name, rssi) in enumerate(device_list, 1):
        print(f"  {i}. {name or '[Sin nombre]'} [{addr}] RSSI: {rssi}")
    
    print("\nSelecciona dispositivo (numero o MAC):")
    sel = input("> ").strip()
    
    try:
        idx = int(sel) - 1
        address = device_list[idx][0]
    except:
        address = sel
    
    print(f"\nConectando a {address}...")
    
    async with BleakClient(address) as client:
        print("Conectado!\n")
        
        # Recopilar todas las características
        all_chars = []
        
        print("=" * 60)
        print("SERVICIOS Y CARACTERISTICAS")
        print("=" * 60)
        
        for service in client.services:
            print(f"\n[Servicio] {service.uuid}")
            for char in service.characteristics:
                props = ", ".join(char.properties)
                print(f"  [Char] {char.uuid}")
                print(f"         Handle: {char.handle}")
                print(f"         Props: {props}")
                all_chars.append(char)
        
        # Leer todas las características que se pueden leer
        print("\n" + "=" * 60)
        print("LEYENDO CARACTERISTICAS")
        print("=" * 60)
        
        for char in all_chars:
            if "read" in char.properties:
                try:
                    value = await client.read_gatt_char(char.uuid)
                    print(f"\n{char.uuid}:")
                    print(f"  Hex: {value.hex()}")
                    # Intentar decodificar como ASCII
                    try:
                        ascii_val = value.decode('ascii', errors='replace')
                        if any(c.isalnum() for c in ascii_val):
                            print(f"  ASCII: {ascii_val}")
                    except:
                        pass
                except Exception as e:
                    print(f"\n{char.uuid}: Error - {e}")
        
        # Suscribirse a TODAS las notificaciones
        print("\n" + "=" * 60)
        print("SUSCRIBIENDO A NOTIFICACIONES")
        print("=" * 60)
        
        received_data = []
        
        def notification_handler(sender, data):
            print(f"\n*** [RX de {sender}] {len(data)} bytes:")
            print(f"    Hex: {data.hex()}")
            try:
                ascii_str = data.decode('ascii', errors='replace')
                if any(c.isalnum() for c in ascii_str):
                    print(f"    ASCII: {ascii_str}")
            except:
                pass
            received_data.append((sender, data))
        
        notify_chars = []
        for char in all_chars:
            if "notify" in char.properties or "indicate" in char.properties:
                try:
                    await client.start_notify(char.uuid, notification_handler)
                    print(f"Suscrito a: {char.uuid}")
                    notify_chars.append(char)
                except Exception as e:
                    print(f"Error suscribiendo a {char.uuid}: {e}")
        
        # Esperar un poco por si hay notificaciones automáticas
        print("\nEsperando notificaciones automaticas (3s)...")
        await asyncio.sleep(3)
        
        # Encontrar características de escritura
        write_chars = [c for c in all_chars if "write" in c.properties or "write-without-response" in c.properties]
        
        print("\n" + "=" * 60)
        print("MODO INTERACTIVO")
        print("=" * 60)
        print("\nComandos:")
        print("  1 - Enviar comando JBD basico (DD A5 03 00 FF FD 77)")
        print("  2 - Enviar PIN 123456 como ASCII")
        print("  3 - Enviar bytes: 01 00")
        print("  4 - Leer todas las caracteristicas de nuevo")
        print("  5 - Probar TODOS los comandos comunes")
        print("  6 - Enviar PIN y luego JBD")
        print("  h XXYYZZ - Enviar hex a TODAS las char de escritura")
        print("  w N XXYYZZ - Escribir en caracteristica N especifica")
        print("  l - Listar caracteristicas de escritura")
        print("  q - Salir")
        
        # Comandos comunes de diferentes BMS
        common_commands = [
            ("JBD Basic 0x03", bytes([0xDD, 0xA5, 0x03, 0x00, 0xFF, 0xFD, 0x77])),
            ("JBD Cells 0x04", bytes([0xDD, 0xA5, 0x04, 0x00, 0xFF, 0xFC, 0x77])),
            ("JBD HW 0x05", bytes([0xDD, 0xA5, 0x05, 0x00, 0xFF, 0xFB, 0x77])),
            ("Daly SOC", bytes([0xA5, 0x40, 0x90, 0x08, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x7D])),
            ("Simple 0x00", bytes([0x00])),
            ("Simple 0x01", bytes([0x01])),
            ("Query 01 00", bytes([0x01, 0x00])),
            ("PIN ASCII", b"123456"),
            ("PIN null term", b"123456\x00"),
            ("PIN hex", bytes([0x12, 0x34, 0x56])),
            ("Btsnoop basic", bytes([0x01, 0x00, 0x01, 0x01, 0x12, 0x00, 0x12, 0x00])),
            ("Btsnoop volt", bytes([0x01, 0x00, 0x01, 0x01, 0x90, 0x04, 0x24, 0x01])),
            ("AT cmd", b"AT\r\n"),
            ("GetInfo", b"GetInfo\r\n"),
        ]
        
        while True:
            try:
                cmd = input("\n> ").strip()
                
                if cmd == 'q':
                    break
                    
                elif cmd == 'l':
                    print("\nCaracteristicas de escritura:")
                    for i, c in enumerate(write_chars):
                        print(f"  {i}: {c.uuid} (handle {c.handle})")
                
                elif cmd == '1':
                    data = bytes([0xDD, 0xA5, 0x03, 0x00, 0xFF, 0xFD, 0x77])
                    print(f"\nEnviando JBD cmd 0x03: {data.hex()}")
                    for wc in write_chars:
                        print(f"  -> {wc.uuid}")
                        try:
                            await client.write_gatt_char(wc.uuid, data, response=False)
                        except Exception as e:
                            print(f"     Error: {e}")
                    await asyncio.sleep(1.0)
                
                elif cmd == '2':
                    data = b"123456"
                    print(f"\nEnviando PIN: {data}")
                    for wc in write_chars:
                        print(f"  -> {wc.uuid}")
                        try:
                            await client.write_gatt_char(wc.uuid, data, response=False)
                        except Exception as e:
                            print(f"     Error: {e}")
                    await asyncio.sleep(1.0)
                
                elif cmd == '3':
                    data = bytes([0x01, 0x00])
                    print(f"\nEnviando: {data.hex()}")
                    for wc in write_chars:
                        print(f"  -> {wc.uuid}")
                        try:
                            await client.write_gatt_char(wc.uuid, data, response=False)
                        except Exception as e:
                            print(f"     Error: {e}")
                    await asyncio.sleep(1.0)
                
                elif cmd == '4':
                    print("\nLeyendo caracteristicas...")
                    for char in all_chars:
                        if "read" in char.properties:
                            try:
                                value = await client.read_gatt_char(char.uuid)
                                print(f"  {char.uuid}: {value.hex()}")
                            except Exception as e:
                                print(f"  {char.uuid}: Error - {e}")
                
                elif cmd == '5':
                    print("\n*** Probando todos los comandos comunes ***")
                    for name, data in common_commands:
                        received_data.clear()
                        print(f"\n--- {name}: {data.hex()} ---")
                        for wc in write_chars:
                            try:
                                await client.write_gatt_char(wc.uuid, data, response=False)
                            except Exception as e:
                                print(f"  Error en {wc.uuid}: {e}")
                        
                        await asyncio.sleep(1.5)
                        
                        if received_data:
                            print(f"  >>> RESPUESTA RECIBIDA! <<<")
                        else:
                            print(f"  (sin respuesta)")
                
                elif cmd == '6':
                    print("\nEnviando PIN primero, luego comando JBD...")
                    
                    # PIN
                    pin = b"123456"
                    for wc in write_chars:
                        try:
                            await client.write_gatt_char(wc.uuid, pin, response=False)
                            print(f"  PIN enviado a {wc.uuid}")
                        except:
                            pass
                    
                    await asyncio.sleep(1.0)
                    
                    # JBD
                    jbd = bytes([0xDD, 0xA5, 0x03, 0x00, 0xFF, 0xFD, 0x77])
                    for wc in write_chars:
                        try:
                            await client.write_gatt_char(wc.uuid, jbd, response=False)
                            print(f"  JBD enviado a {wc.uuid}")
                        except:
                            pass
                    
                    await asyncio.sleep(1.5)
                
                elif cmd.startswith('h '):
                    try:
                        hex_str = cmd[2:].replace(' ', '')
                        data = bytes.fromhex(hex_str)
                        print(f"\nEnviando: {data.hex()}")
                        for wc in write_chars:
                            print(f"  -> {wc.uuid}")
                            try:
                                await client.write_gatt_char(wc.uuid, data, response=False)
                            except Exception as e:
                                print(f"     Error: {e}")
                        await asyncio.sleep(1.0)
                    except ValueError:
                        print("Formato hex invalido")
                
                elif cmd.startswith('w '):
                    try:
                        parts = cmd[2:].split(' ', 1)
                        idx = int(parts[0])
                        hex_str = parts[1].replace(' ', '')
                        data = bytes.fromhex(hex_str)
                        wc = write_chars[idx]
                        print(f"\nEnviando a {wc.uuid}: {data.hex()}")
                        await client.write_gatt_char(wc.uuid, data, response=False)
                        await asyncio.sleep(1.0)
                    except Exception as e:
                        print(f"Error: {e}")
                
                else:
                    print("Comando no reconocido")
                    
            except KeyboardInterrupt:
                break
            except EOFError:
                break
        
        # Limpiar suscripciones
        for char in notify_chars:
            try:
                await client.stop_notify(char.uuid)
            except:
                pass


if __name__ == "__main__":
    asyncio.run(main())
