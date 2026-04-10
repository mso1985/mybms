#!/usr/bin/env python3
"""
Analizador de archivo BTSnoop para extraer protocolo BLE
Especialmente diseñado para capturar comandos de apps BMS como Smart BTS
"""

import struct
import sys
from dataclasses import dataclass
from typing import List, Optional
from collections import defaultdict


@dataclass
class BTSnoopHeader:
    """Cabecera del archivo btsnoop"""
    identification: bytes  # "btsnoop\0"
    version: int
    datalink_type: int


@dataclass  
class BTSnoopPacket:
    """Paquete individual del archivo btsnoop"""
    original_length: int
    included_length: int
    packet_flags: int
    cumulative_drops: int
    timestamp: int
    data: bytes
    
    @property
    def direction(self) -> str:
        """Direccion del paquete (sent/received)"""
        return "sent" if (self.packet_flags & 0x01) == 0 else "received"
    
    @property
    def is_command(self) -> bool:
        """Es un comando HCI"""
        return (self.packet_flags & 0x02) != 0


class ATTPacket:
    """Paquete ATT (Attribute Protocol)"""
    
    OPCODES = {
        0x01: "ERROR_RSP",
        0x02: "MTU_REQ",
        0x03: "MTU_RSP", 
        0x04: "FIND_INFO_REQ",
        0x05: "FIND_INFO_RSP",
        0x06: "FIND_BY_TYPE_REQ",
        0x07: "FIND_BY_TYPE_RSP",
        0x08: "READ_BY_TYPE_REQ",
        0x09: "READ_BY_TYPE_RSP",
        0x0A: "READ_REQ",
        0x0B: "READ_RSP",
        0x0C: "READ_BLOB_REQ",
        0x0D: "READ_BLOB_RSP",
        0x10: "READ_BY_GROUP_REQ",
        0x11: "READ_BY_GROUP_RSP",
        0x12: "WRITE_REQ",
        0x13: "WRITE_RSP",
        0x16: "PREPARE_WRITE_REQ",
        0x17: "PREPARE_WRITE_RSP",
        0x18: "EXECUTE_WRITE_REQ",
        0x19: "EXECUTE_WRITE_RSP",
        0x1B: "HANDLE_VALUE_NTF",
        0x1D: "HANDLE_VALUE_IND",
        0x1E: "HANDLE_VALUE_CFM",
        0x52: "WRITE_CMD",
    }
    
    def __init__(self, data: bytes):
        self.data = data
        self.opcode = data[0] if data else 0
        self.opcode_name = self.OPCODES.get(self.opcode, f"UNKNOWN_{self.opcode:#x}")
        
    @property
    def handle(self) -> Optional[int]:
        """Handle del atributo (si aplica)"""
        if len(self.data) >= 3 and self.opcode in [0x0A, 0x0B, 0x12, 0x13, 0x52, 0x1B]:
            return struct.unpack('<H', self.data[1:3])[0]
        return None
        
    @property
    def payload(self) -> bytes:
        """Datos del payload"""
        if self.opcode in [0x12, 0x52]:  # Write Request/Command
            return self.data[3:] if len(self.data) > 3 else b''
        elif self.opcode == 0x1B:  # Notification
            return self.data[3:] if len(self.data) > 3 else b''
        return self.data[1:]


def parse_btsnoop_header(data: bytes) -> BTSnoopHeader:
    """Parsea la cabecera del archivo btsnoop"""
    identification = data[0:8]
    version = struct.unpack('>I', data[8:12])[0]
    datalink_type = struct.unpack('>I', data[12:16])[0]
    
    return BTSnoopHeader(identification, version, datalink_type)


def parse_btsnoop_packets(data: bytes, offset: int = 16) -> List[BTSnoopPacket]:
    """Parsea todos los paquetes del archivo btsnoop"""
    packets = []
    
    while offset < len(data):
        if offset + 24 > len(data):
            break
            
        # Cabecera del paquete (24 bytes)
        original_length = struct.unpack('>I', data[offset:offset+4])[0]
        included_length = struct.unpack('>I', data[offset+4:offset+8])[0]
        packet_flags = struct.unpack('>I', data[offset+8:offset+12])[0]
        cumulative_drops = struct.unpack('>I', data[offset+12:offset+16])[0]
        timestamp = struct.unpack('>Q', data[offset+16:offset+24])[0]
        
        # Datos del paquete
        packet_data = data[offset+24:offset+24+included_length]
        
        packets.append(BTSnoopPacket(
            original_length=original_length,
            included_length=included_length,
            packet_flags=packet_flags,
            cumulative_drops=cumulative_drops,
            timestamp=timestamp,
            data=packet_data
        ))
        
        offset += 24 + included_length
        
    return packets


def extract_l2cap_att(packet: BTSnoopPacket) -> Optional[ATTPacket]:
    """Extrae paquete ATT de un paquete HCI/L2CAP"""
    data = packet.data
    
    if len(data) < 5:
        return None
    
    # Buscar paquetes ACL (tipo 0x02)
    if data[0] != 0x02:
        return None
        
    # Saltar cabecera HCI ACL (4 bytes) y L2CAP (4 bytes)
    # HCI ACL: handle(2) + length(2)
    # L2CAP: length(2) + CID(2)
    
    if len(data) < 9:
        return None
        
    # L2CAP CID 0x0004 = ATT
    l2cap_cid = struct.unpack('<H', data[6:8])[0]
    
    if l2cap_cid != 0x0004:
        return None
        
    att_data = data[8:]
    
    if len(att_data) < 1:
        return None
        
    return ATTPacket(att_data)


def analyze_btsnoop(filename: str):
    """Analiza un archivo btsnoop y extrae comandos ATT"""
    
    print(f"Analizando archivo: {filename}")
    print("=" * 60)
    
    with open(filename, 'rb') as f:
        data = f.read()
    
    # Parsear cabecera
    header = parse_btsnoop_header(data)
    print(f"Identificacion: {header.identification}")
    print(f"Version: {header.version}")
    print(f"Datalink Type: {header.datalink_type}")
    print()
    
    # Parsear paquetes
    packets = parse_btsnoop_packets(data)
    print(f"Total de paquetes: {len(packets)}")
    print()
    
    # Extraer paquetes ATT
    att_packets = []
    for pkt in packets:
        att = extract_l2cap_att(pkt)
        if att:
            att_packets.append((pkt, att))
    
    print(f"Paquetes ATT encontrados: {len(att_packets)}")
    print()
    
    # Agrupar por handle y opcode
    writes_by_handle = defaultdict(list)
    notifications_by_handle = defaultdict(list)
    
    print("=" * 60)
    print("COMANDOS WRITE (enviados a la bateria):")
    print("=" * 60)
    
    for pkt, att in att_packets:
        if att.opcode in [0x12, 0x52]:  # Write Request/Command
            handle = att.handle
            payload = att.payload
            if handle and payload:
                writes_by_handle[handle].append(payload)
                print(f"[{pkt.direction:8}] Handle: {handle:#06x} | Opcode: {att.opcode_name}")
                print(f"           Payload: {payload.hex()}")
                
                # Intentar decodificar
                if payload:
                    decode_bms_command(payload)
                print()
    
    print()
    print("=" * 60)
    print("NOTIFICACIONES (recibidas de la bateria):")
    print("=" * 60)
    
    for pkt, att in att_packets:
        if att.opcode == 0x1B:  # Notification
            handle = att.handle
            payload = att.payload
            if handle and payload:
                notifications_by_handle[handle].append(payload)
                print(f"[{pkt.direction:8}] Handle: {handle:#06x}")
                print(f"           Data: {payload.hex()}")
                
                # Intentar decodificar
                if payload:
                    decode_bms_response(payload)
                print()
    
    # Resumen
    print()
    print("=" * 60)
    print("RESUMEN DE HANDLES:")
    print("=" * 60)
    
    print("\nHandles de escritura utilizados:")
    for handle, payloads in sorted(writes_by_handle.items()):
        print(f"  Handle {handle:#06x}: {len(payloads)} escrituras")
        # Mostrar comandos unicos
        unique_cmds = set(p.hex() for p in payloads)
        print(f"    Comandos unicos: {len(unique_cmds)}")
        for cmd in list(unique_cmds)[:5]:  # Mostrar max 5
            print(f"      - {cmd}")
    
    print("\nHandles de notificacion utilizados:")
    for handle, payloads in sorted(notifications_by_handle.items()):
        print(f"  Handle {handle:#06x}: {len(payloads)} notificaciones")
    
    # Generar codigo Python
    print()
    print("=" * 60)
    print("CODIGO PYTHON GENERADO:")
    print("=" * 60)
    
    generate_python_code(writes_by_handle, notifications_by_handle)


def decode_bms_command(data: bytes):
    """Intenta decodificar un comando de BMS"""
    if len(data) < 1:
        return
        
    # Detectar protocolo JBD/Xiaoxiang (empieza con DD A5)
    if len(data) >= 2 and data[0] == 0xDD and data[1] == 0xA5:
        cmd = data[2] if len(data) > 2 else 0
        print(f"           [JBD] Comando: {cmd:#04x}", end="")
        if cmd == 0x03:
            print(" (Info basica)")
        elif cmd == 0x04:
            print(" (Voltajes de celdas)")
        elif cmd == 0x05:
            print(" (Hardware info)")
        else:
            print()
            
    # Detectar protocolo Daly (empieza con A5)
    elif data[0] == 0xA5:
        cmd = data[1] if len(data) > 1 else 0
        print(f"           [DALY] Comando: {cmd:#04x}")
        
    # Otros patrones
    else:
        # Buscar patrones conocidos
        if data[0:2] == bytes([0x01, 0x00]):
            print(f"           [CUSTOM] Posible comando de lectura")
        elif len(data) >= 4 and data[0] == 0x01:
            print(f"           [CUSTOM] Comando tipo 0x01")


def decode_bms_response(data: bytes):
    """Intenta decodificar una respuesta de BMS"""
    if len(data) < 4:
        return
        
    # Respuesta JBD (empieza con DD)
    if data[0] == 0xDD:
        cmd = data[1]
        status = data[2]
        length = data[3]
        print(f"           [JBD] Respuesta cmd={cmd:#04x} status={status:#04x} len={length}")
        
        if cmd == 0x03 and len(data) >= 27:  # Info basica
            voltage = struct.unpack('>H', data[4:6])[0] / 100.0
            current = struct.unpack('>h', data[6:8])[0] / 100.0
            cap_remain = struct.unpack('>H', data[8:10])[0] / 100.0
            cap_full = struct.unpack('>H', data[10:12])[0] / 100.0
            cycles = struct.unpack('>H', data[12:14])[0]
            
            print(f"           Voltaje: {voltage:.2f}V, Corriente: {current:.2f}A")
            print(f"           Capacidad: {cap_remain:.2f}/{cap_full:.2f}Ah")
            print(f"           Ciclos: {cycles}")
            
        elif cmd == 0x04:  # Voltajes de celdas
            cell_count = (len(data) - 6) // 2
            print(f"           Celdas: {cell_count}")
            for i in range(min(cell_count, 16)):
                v = struct.unpack('>H', data[4+i*2:6+i*2])[0] / 1000.0
                print(f"             Celda {i+1}: {v:.3f}V")
    
    # Intentar decodificar valores comunes
    else:
        # Buscar voltajes (tipicamente 2 bytes, 3000-4200 mV por celda)
        for i in range(0, len(data)-1, 2):
            val = struct.unpack('>H', data[i:i+2])[0]
            if 2500 < val < 4500:  # Rango tipico de celda Li-ion
                print(f"           Posible voltaje celda en offset {i}: {val}mV ({val/1000.0:.3f}V)")
            elif 10000 < val < 80000:  # Rango tipico de pack
                print(f"           Posible voltaje pack en offset {i}: {val/100.0:.2f}V")


def generate_python_code(writes: dict, notifications: dict):
    """Genera codigo Python basado en los comandos detectados"""
    
    print("""
# UUIDs detectados (ajustar segun tu dispositivo)
# Estos handles corresponden a las caracteristicas GATT""")
    
    for handle in sorted(writes.keys()):
        print(f"WRITE_HANDLE = {handle:#06x}  # Handle de escritura")
        
    for handle in sorted(notifications.keys()):
        print(f"NOTIFY_HANDLE = {handle:#06x}  # Handle de notificacion")
    
    print("""
# Comandos detectados:""")
    
    for handle, payloads in sorted(writes.items()):
        unique_cmds = list(set(p.hex() for p in payloads))
        for i, cmd in enumerate(unique_cmds[:10]):
            print(f"CMD_{i} = bytes.fromhex('{cmd}')")
    
    print("""
# Ejemplo de uso con bleak:
async def send_command(client, command):
    await client.write_gatt_char(WRITE_HANDLE, command, response=False)
    await asyncio.sleep(0.3)  # Esperar respuesta
""")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        # Usar archivo por defecto
        filename = "snoop.log"
    else:
        filename = sys.argv[1]
    
    try:
        analyze_btsnoop(filename)
    except FileNotFoundError:
        print(f"Error: No se encontro el archivo {filename}")
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
