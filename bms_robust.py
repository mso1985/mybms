#!/usr/bin/env python3
"""
BMS Robust Connector - Solución al problema de respuesta vacía

Este script implementa múltiples estrategias para resolver el problema
de respuestas vacías del BMS:

1. Sistema de reintentos con backoff exponencial
2. Comandos de wake-up para BMS en modo sleep
3. Múltiples formatos de comandos (JBD, DALY, JK, custom)
4. Detección inteligente de fin de mensaje
5. Delays configurables entre operaciones
6. Modo de diagnóstico detallado

Uso:
    python bms_robust.py --mac XX:XX:XX:XX:XX:XX
    python bms_robust.py --mac XX:XX:XX:XX:XX:XX --diagnose
    python bms_robust.py --mac XX:XX:XX:XX:XX:XX --retries 5 --timeout 10
"""

import asyncio
import struct
import sys
import argparse
import time
from typing import Optional, List, Dict, Any, Tuple
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import logging

try:
    from bleak import BleakClient, BleakScanner
    from bleak.backends.characteristic import BleakGATTCharacteristic
except ImportError:
    print("Error: Se requiere instalar 'bleak'")
    print("Ejecuta: pip install bleak")
    sys.exit(1)

# Configurar logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class Protocol(Enum):
    """Protocolos de BMS soportados"""
    JBD = "jbd"           # Xiaoxiang/Smart BMS
    DALY = "daly"         # DALY BMS
    JK = "jk"             # Jikong BMS  
    HM10 = "hm10"         # Módulos HM-10/BL
    CUSTOM = "custom"     # Protocolo personalizado
    UNKNOWN = "unknown"


class ResponseStatus(Enum):
    """Estado de la respuesta"""
    SUCCESS = "success"
    TIMEOUT = "timeout"
    EMPTY = "empty"
    INVALID = "invalid"
    ERROR = "error"


@dataclass
class BMSData:
    """Datos del BMS"""
    timestamp: datetime
    voltage_v: float = 0.0
    current_a: float = 0.0
    capacity_ah: float = 0.0
    capacity_percent: int = 0
    temperature_c: List[float] = field(default_factory=list)
    cell_voltages: List[float] = field(default_factory=list)
    protection_status: Dict[str, bool] = field(default_factory=dict)
    charge_cycles: int = 0
    raw_response: bytes = b''
    protocol_used: str = ""
    
    def __str__(self) -> str:
        temps = ", ".join([f"{t:.1f}°C" for t in self.temperature_c]) if self.temperature_c else "N/A"
        cells = ", ".join([f"{v:.3f}V" for v in self.cell_voltages[:8]]) if self.cell_voltages else "N/A"
        if len(self.cell_voltages) > 8:
            cells += f" ... ({len(self.cell_voltages)} celdas)"
        return f"""
╔══════════════════════════════════════════════════════════╗
║         DATOS BMS [{self.timestamp.strftime('%Y-%m-%d %H:%M:%S')}]          ║
╠══════════════════════════════════════════════════════════╣
║ Voltaje Total:     {self.voltage_v:>8.3f} V                       ║
║ Corriente:         {self.current_a:>8.3f} A                       ║
║ Capacidad:         {self.capacity_ah:>8.3f} Ah                      ║
║ SOC:               {self.capacity_percent:>8}%                        ║
║ Ciclos de Carga:   {self.charge_cycles:>8}                         ║
║ Temperaturas:      {temps:<36} ║
║ Protocolo:         {self.protocol_used:<36} ║
╠══════════════════════════════════════════════════════════╣
║ Celdas: {cells:<48} ║
╚══════════════════════════════════════════════════════════╝"""


@dataclass
class CommandResult:
    """Resultado de un comando enviado"""
    status: ResponseStatus
    response: Optional[bytes] = None
    latency_ms: float = 0
    retries_needed: int = 0
    protocol_used: str = ""
    error_message: str = ""


class BMSRobustConnector:
    """
    Conector robusto para BMS Bluetooth con manejo mejorado de respuestas vacías
    """
    
    # Definición de UUIDs para diferentes tipos de BMS
    UUID_PROFILES = {
        'jbd': {
            'service': "0000ff00-0000-1000-8000-00805f9b34fb",
            'write': "0000ff02-0000-1000-8000-00805f9b34fb",
            'notify': "0000ff01-0000-1000-8000-00805f9b34fb"
        },
        'hm10': {
            'service': "0000ffe0-0000-1000-8000-00805f9b34fb",
            'write': "0000ffe1-0000-1000-8000-00805f9b34fb",
            'notify': "0000ffe1-0000-1000-8000-00805f9b34fb"
        },
        'daly': {
            'service': "0000fff0-0000-1000-8000-00805f9b34fb",
            'write': "0000fff2-0000-1000-8000-00805f9b34fb",
            'notify': "0000fff1-0000-1000-8000-00805f9b34fb"
        },
        'custom': {
            'service': "02f00000-0000-0000-0000-00000000fe00",
            'write': "02f00000-0000-0000-0000-00000000ff01",
            'notify': "02f00000-0000-0000-0000-00000000ff02"
        },
        'custom_alt': {
            'service': "02f00000-0000-0000-0000-00000000fe00",
            'write': "02f00000-0000-0000-0000-00000000ff04",
            'notify': "02f00000-0000-0000-0000-00000000ff04"
        }
    }
    
    # Comandos de wake-up conocidos
    WAKE_UP_COMMANDS = [
        bytes([0xDD, 0xA5, 0x05, 0x00, 0xAA, 0x77]),  # JBD wake
        bytes([0xDD, 0xA5, 0x00, 0x00, 0xA5, 0x77]),  # JBD status
        bytes([0x00]),                                 # Simple trigger
        bytes([0xDD]),                                 # Header only
        bytes([0xA5, 0x40, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0xF0]),  # DALY wake
    ]
    
    def __init__(
        self,
        mac_address: str,
        max_retries: int = 5,
        base_timeout: float = 3.0,
        connection_delay: float = 1.0,
        command_delay: float = 0.3,
        backoff_factor: float = 1.5,
        verbose: bool = False
    ):
        self.mac_address = mac_address
        self.max_retries = max_retries
        self.base_timeout = base_timeout
        self.connection_delay = connection_delay
        self.command_delay = command_delay
        self.backoff_factor = backoff_factor
        self.verbose = verbose
        
        self.client: Optional[BleakClient] = None
        self.response_buffer = bytearray()
        self.command_event = asyncio.Event()
        self.last_response: Optional[bytes] = None
        
        # UUIDs detectados
        self.write_uuid: Optional[str] = None
        self.notify_uuid: Optional[str] = None
        self.detected_protocol: Protocol = Protocol.UNKNOWN
        
        # Estadísticas
        self.stats = {
            'commands_sent': 0,
            'responses_received': 0,
            'timeouts': 0,
            'empty_responses': 0,
            'successful_reads': 0
        }
    
    def _log(self, message: str, level: str = "info"):
        """Log con nivel configurable"""
        if self.verbose or level in ["error", "warning"]:
            if level == "error":
                logger.error(message)
            elif level == "warning":
                logger.warning(message)
            else:
                logger.info(message)
    
    # ==================== CONSTRUCCIÓN DE COMANDOS ====================
    
    def _build_jbd_command(self, register: int, data: bytes = b'') -> bytes:
        """Construir comando JBD/Smart BMS"""
        length = len(data)
        packet = bytes([0xDD, 0xA5, register, length]) + data
        crc = sum(packet[2:]) & 0xFF
        return packet + bytes([crc, 0x77])
    
    def _build_jbd_command_v2(self, register: int) -> bytes:
        """Comando JBD alternativo con CRC diferente"""
        packet = bytes([0xDD, 0xA5, register, 0x00])
        crc = (0x10000 - sum(packet[2:])) & 0xFF  # Complemento
        return packet + bytes([crc, 0x77])
    
    def _build_daly_command(self, command: int) -> bytes:
        """Construir comando DALY"""
        # DALY: A5 [address] [cmd] [length=8] [data x8] [checksum]
        data = bytes([0x00] * 8)
        frame = bytes([0xA5, 0x40, command, 0x08]) + data
        checksum = sum(frame) & 0xFF
        return frame + bytes([checksum])
    
    def _build_jk_command(self, command: int) -> bytes:
        """Construir comando JK"""
        # JK BMS protocol
        return bytes([
            0x4E, 0x57, 0x00, 0x13, 0x00, 0x00, 0x00, 0x00,
            0x06, command, 0x00, 0x00, 0x00, 0x00, 0x00, 0x68,
            0x00, 0x00, 0x01, 0x29
        ])
    
    def _build_simple_command(self, register: int) -> bytes:
        """Comando simplificado sin CRC"""
        return bytes([0xDD, 0xA5, register, 0x00, 0x00, 0x77])
    
    def _get_all_command_variants(self, register: int) -> List[Tuple[str, bytes]]:
        """Obtener todas las variantes de comando para un registro"""
        variants = [
            ("JBD estándar", self._build_jbd_command(register)),
            ("JBD v2 (CRC complemento)", self._build_jbd_command_v2(register)),
            ("JBD simplificado", self._build_simple_command(register)),
            ("DALY", self._build_daly_command(0x90 if register == 0x03 else 0x95)),
            ("JK", self._build_jk_command(register)),
        ]
        
        # Agregar variantes adicionales para el registro 0x03
        if register == 0x03:
            variants.extend([
                ("JBD sin CRC", bytes([0xDD, 0xA5, 0x03, 0x00, 0x77])),
                ("JBD minimal", bytes([0xDD, 0xA5, 0x03])),
                ("Trigger simple", bytes([0x03])),
            ])
        
        return variants
    
    # ==================== MANEJO DE NOTIFICACIONES ====================
    
    def _notification_handler(self, sender: BleakGATTCharacteristic, data: bytearray):
        """Manejar notificaciones BLE con detección inteligente de fin de mensaje"""
        self.response_buffer.extend(data)
        
        if self.verbose:
            self._log(f"  [RX] {len(data)} bytes: {data.hex()}")
        
        # Detectar fin de mensaje según diferentes protocolos
        if self._is_message_complete():
            self.last_response = bytes(self.response_buffer)
            self.response_buffer = bytearray()
            self.command_event.set()
            self.stats['responses_received'] += 1
    
    def _is_message_complete(self) -> bool:
        """Detectar si el mensaje está completo según el protocolo"""
        buf = self.response_buffer
        
        if len(buf) < 4:
            return False
        
        # JBD: termina con 0x77
        if buf[0] == 0xDD:
            if buf[-1] == 0x77 and len(buf) >= 7:
                return True
            # También verificar longitud del payload
            if len(buf) > 3:
                expected_len = buf[3] + 7  # header(4) + payload + crc(1) + end(1) + 1
                if len(buf) >= expected_len:
                    return True
        
        # DALY: longitud fija de 13 bytes típicamente
        if buf[0] == 0xA5 and len(buf) >= 13:
            return True
        
        # JK: header 0x4E 0x57, típicamente > 300 bytes
        if len(buf) >= 2 and buf[0] == 0x4E and buf[1] == 0x57:
            if len(buf) >= 300:
                return True
        
        # Fallback: si hay suficientes datos (>=30 bytes) asumir completo
        if len(buf) >= 30:
            return True
        
        return False
    
    # ==================== CONEXIÓN Y DETECCIÓN ====================
    
    async def _detect_uuids(self) -> bool:
        """Detectar UUIDs del BMS automáticamente"""
        if not self.client:
            return False
        
        self._log("Detectando UUIDs y protocolo del BMS...")
        
        for service in self.client.services:
            service_uuid = service.uuid.lower()
            
            # Buscar coincidencia con perfiles conocidos
            for profile_name, profile in self.UUID_PROFILES.items():
                if profile['service'].lower() in service_uuid or \
                   service_uuid in profile['service'].lower():
                    
                    for char in service.characteristics:
                        char_uuid = char.uuid.lower()
                        
                        # Detectar write UUID
                        if profile['write'].lower() in char_uuid or \
                           char_uuid in profile['write'].lower():
                            if 'write' in char.properties:
                                self.write_uuid = char.uuid
                                self._log(f"  Write UUID detectado: {char.uuid}")
                        
                        # Detectar notify UUID
                        if profile['notify'].lower() in char_uuid or \
                           char_uuid in profile['notify'].lower():
                            if 'notify' in char.properties:
                                self.notify_uuid = char.uuid
                                self._log(f"  Notify UUID detectado: {char.uuid}")
                    
                    if self.write_uuid and self.notify_uuid:
                        self.detected_protocol = Protocol(profile_name) if profile_name in [p.value for p in Protocol] else Protocol.CUSTOM
                        self._log(f"  Protocolo detectado: {self.detected_protocol.value}")
                        return True
        
        # Fallback: buscar cualquier UUID con write/notify
        self._log("  Buscando UUIDs genéricos...")
        for service in self.client.services:
            for char in service.characteristics:
                if 'write' in char.properties and not self.write_uuid:
                    self.write_uuid = char.uuid
                    self._log(f"  Write UUID (genérico): {char.uuid}")
                if 'notify' in char.properties and not self.notify_uuid:
                    self.notify_uuid = char.uuid
                    self._log(f"  Notify UUID (genérico): {char.uuid}")
        
        return bool(self.write_uuid and self.notify_uuid)
    
    async def connect(self) -> bool:
        """Conectar al BMS con manejo robusto"""
        self._log(f"Conectando a {self.mac_address}...")
        
        try:
            self.client = BleakClient(self.mac_address)
            await self.client.connect()
            
            if not self.client.is_connected:
                self._log("Error: Conexión fallida", "error")
                return False
            
            self._log(f"Conectado a: {self.client.name or 'BMS'}")
            
            # Esperar antes de detectar servicios
            await asyncio.sleep(self.connection_delay)
            
            # Detectar UUIDs
            if not await self._detect_uuids():
                self._log("ADVERTENCIA: No se detectaron UUIDs automáticamente", "warning")
                return False
            
            # Configurar notificaciones
            try:
                await self.client.start_notify(self.notify_uuid, self._notification_handler)
                self._log("Notificaciones configuradas")
            except Exception as e:
                self._log(f"Error configurando notificaciones: {e}", "error")
                return False
            
            # Esperar después de configurar notificaciones
            await asyncio.sleep(self.connection_delay / 2)
            
            return True
            
        except Exception as e:
            self._log(f"Error de conexión: {e}", "error")
            return False
    
    async def disconnect(self):
        """Desconectar del BMS"""
        if self.client and self.client.is_connected:
            try:
                await self.client.disconnect()
                self._log("Desconectado del BMS")
            except Exception as e:
                self._log(f"Error al desconectar: {e}", "warning")
    
    # ==================== WAKE-UP DEL BMS ====================
    
    async def wake_up_bms(self) -> bool:
        """Intentar despertar el BMS si está en modo sleep"""
        self._log("Intentando despertar el BMS...")
        
        for i, wake_cmd in enumerate(self.WAKE_UP_COMMANDS):
            try:
                self._log(f"  Wake-up intento {i+1}: {wake_cmd.hex()}")
                await self.client.write_gatt_char(
                    self.write_uuid,
                    wake_cmd,
                    response=False
                )
                await asyncio.sleep(0.2)
            except Exception as e:
                self._log(f"  Error en wake-up {i+1}: {e}", "warning")
        
        # Esperar a que el BMS responda
        await asyncio.sleep(1.0)
        return True
    
    # ==================== ENVÍO DE COMANDOS ====================
    
    async def _send_single_command(
        self,
        command: bytes,
        description: str,
        timeout: float
    ) -> CommandResult:
        """Enviar un solo comando y esperar respuesta"""
        self.command_event.clear()
        self.response_buffer = bytearray()
        self.last_response = None
        
        start_time = time.time()
        
        try:
            self.stats['commands_sent'] += 1
            
            if self.verbose:
                self._log(f"  [TX] {description}: {command.hex()}")
            
            await self.client.write_gatt_char(
                self.write_uuid,
                command,
                response=False
            )
            
            try:
                await asyncio.wait_for(self.command_event.wait(), timeout=timeout)
                latency = (time.time() - start_time) * 1000
                
                if self.last_response and len(self.last_response) > 0:
                    return CommandResult(
                        status=ResponseStatus.SUCCESS,
                        response=self.last_response,
                        latency_ms=latency,
                        protocol_used=description
                    )
                else:
                    self.stats['empty_responses'] += 1
                    return CommandResult(
                        status=ResponseStatus.EMPTY,
                        latency_ms=latency,
                        protocol_used=description
                    )
                    
            except asyncio.TimeoutError:
                self.stats['timeouts'] += 1
                return CommandResult(
                    status=ResponseStatus.TIMEOUT,
                    latency_ms=(time.time() - start_time) * 1000,
                    protocol_used=description
                )
                
        except Exception as e:
            return CommandResult(
                status=ResponseStatus.ERROR,
                error_message=str(e),
                protocol_used=description
            )
    
    async def send_command_with_retry(
        self,
        register: int,
        description: str = ""
    ) -> CommandResult:
        """
        Enviar comando con reintentos y múltiples variantes de protocolo
        
        Esta es la función principal que resuelve el problema de respuesta vacía
        mediante:
        1. Reintentos con backoff exponencial
        2. Prueba de múltiples formatos de comando
        3. Wake-up automático si es necesario
        """
        
        # Obtener todas las variantes de comando
        command_variants = self._get_all_command_variants(register)
        
        # Si tenemos un protocolo detectado, priorizar ese formato
        if self.detected_protocol != Protocol.UNKNOWN:
            # Reordenar para probar primero el protocolo detectado
            priority_order = {
                Protocol.JBD: ["JBD estándar", "JBD v2", "JBD simplificado"],
                Protocol.DALY: ["DALY"],
                Protocol.JK: ["JK"],
                Protocol.CUSTOM: ["JBD estándar", "JBD simplificado"],
            }
            priority = priority_order.get(self.detected_protocol, [])
            command_variants.sort(
                key=lambda x: priority.index(x[0]) if x[0] in priority else 999
            )
        
        best_result = None
        total_retries = 0
        wake_up_attempted = False
        
        for retry in range(self.max_retries):
            timeout = self.base_timeout * (self.backoff_factor ** retry)
            
            # Intentar wake-up después de algunos timeouts
            if retry == 2 and not wake_up_attempted:
                await self.wake_up_bms()
                wake_up_attempted = True
            
            for variant_name, command in command_variants:
                result = await self._send_single_command(
                    command,
                    variant_name,
                    timeout
                )
                
                if result.status == ResponseStatus.SUCCESS:
                    result.retries_needed = total_retries
                    self.stats['successful_reads'] += 1
                    self._log(f"✓ Éxito con {variant_name} después de {total_retries} reintentos")
                    return result
                
                total_retries += 1
                
                # Guardar el mejor resultado parcial
                if best_result is None or result.status.value < best_result.status.value:
                    best_result = result
                
                # Delay entre comandos
                await asyncio.sleep(self.command_delay)
            
            # Delay adicional entre rondas de reintentos
            if retry < self.max_retries - 1:
                self._log(f"  Reintento {retry + 2}/{self.max_retries}...")
                await asyncio.sleep(self.command_delay * 2)
        
        # Si no hubo éxito, retornar el mejor resultado obtenido
        if best_result:
            best_result.retries_needed = total_retries
            return best_result
        
        return CommandResult(
            status=ResponseStatus.TIMEOUT,
            retries_needed=total_retries,
            error_message=f"No se obtuvo respuesta después de {total_retries} intentos"
        )
    
    # ==================== PARSEO DE DATOS ====================
    
    def _parse_jbd_basic_info(self, data: bytes) -> Optional[BMSData]:
        """Parsear información básica JBD"""
        try:
            # Buscar el inicio del payload (después del header)
            payload = data
            if len(data) > 4 and data[0] == 0xDD:
                payload_length = data[3]
                payload = data[4:4+payload_length]
            
            if len(payload) < 25:
                self._log(f"Payload muy corto: {len(payload)} bytes", "warning")
                return None
            
            voltage = struct.unpack('>H', payload[0:2])[0] / 100.0
            current = struct.unpack('>h', payload[2:4])[0] / 100.0
            capacity_remain = struct.unpack('>H', payload[4:6])[0] / 100.0
            capacity_total = struct.unpack('>H', payload[6:8])[0] / 100.0
            cycles = struct.unpack('>H', payload[8:10])[0]
            
            num_temps = payload[22] if len(payload) > 22 else 0
            temperatures = []
            for i in range(min(num_temps, 4)):
                if len(payload) >= 25 + i*2:
                    temp_raw = struct.unpack('>h', payload[23 + i*2:25 + i*2])[0]
                    temp = (temp_raw - 2731) / 10.0
                    temperatures.append(temp)
            
            soc = int((capacity_remain / capacity_total) * 100) if capacity_total > 0 else 0
            
            return BMSData(
                timestamp=datetime.now(),
                voltage_v=voltage,
                current_a=current,
                capacity_ah=capacity_remain,
                capacity_percent=soc,
                temperature_c=temperatures,
                charge_cycles=cycles,
                raw_response=data,
                protocol_used="JBD"
            )
        except Exception as e:
            self._log(f"Error parseando JBD: {e}", "error")
            return None
    
    def _parse_daly_basic_info(self, data: bytes) -> Optional[BMSData]:
        """Parsear información básica DALY"""
        try:
            if len(data) < 13:
                return None
            
            # Buscar payload después del header DALY
            payload = data[4:] if data[0] == 0xA5 else data
            
            voltage = struct.unpack('>H', payload[0:2])[0] / 10.0
            current_raw = struct.unpack('>H', payload[2:4])[0]
            current = (current_raw - 30000) / 10.0
            soc = payload[8] if len(payload) > 8 else 0
            
            return BMSData(
                timestamp=datetime.now(),
                voltage_v=voltage,
                current_a=current,
                capacity_percent=soc,
                raw_response=data,
                protocol_used="DALY"
            )
        except Exception as e:
            self._log(f"Error parseando DALY: {e}", "error")
            return None
    
    def _parse_response(self, data: bytes) -> Optional[BMSData]:
        """Parsear respuesta según el protocolo detectado"""
        if not data or len(data) < 4:
            return None
        
        # Detectar tipo de respuesta y parsear
        if data[0] == 0xDD:
            return self._parse_jbd_basic_info(data)
        elif data[0] == 0xA5:
            return self._parse_daly_basic_info(data)
        else:
            # Intentar parsear como JBD directamente
            return self._parse_jbd_basic_info(data)
    
    def _parse_cell_voltages(self, data: bytes) -> List[float]:
        """Parsear voltajes de celdas"""
        voltages = []
        try:
            payload = data
            if len(data) > 4 and data[0] == 0xDD:
                payload_length = data[3]
                payload = data[4:4+payload_length]
            
            for i in range(0, len(payload) - 1, 2):
                v = struct.unpack('>H', payload[i:i+2])[0] / 1000.0
                if 0.5 < v < 5.0:  # Filtrar valores válidos
                    voltages.append(v)
        except Exception as e:
            self._log(f"Error parseando voltajes: {e}", "error")
        return voltages
    
    # ==================== FUNCIONES PRINCIPALES ====================
    
    async def read_bms_data(self) -> Optional[BMSData]:
        """
        Leer datos del BMS con manejo robusto de respuestas vacías
        
        Returns:
            BMSData si la lectura es exitosa, None en caso contrario
        """
        if not self.client or not self.client.is_connected:
            self._log("Error: No hay conexión activa", "error")
            return None
        
        # Solicitar información básica
        self._log("Solicitando información básica...")
        result = await self.send_command_with_retry(0x03, "basic_info")
        
        if result.status != ResponseStatus.SUCCESS:
            self._log(f"Error obteniendo datos: {result.status.value}", "error")
            if result.error_message:
                self._log(f"  Detalle: {result.error_message}", "error")
            return None
        
        bms_data = self._parse_response(result.response)
        if not bms_data:
            self._log("Error: No se pudieron parsear los datos", "error")
            return None
        
        bms_data.protocol_used = result.protocol_used
        
        # Intentar obtener voltajes de celdas
        await asyncio.sleep(self.command_delay)
        
        self._log("Solicitando voltajes de celdas...")
        cell_result = await self.send_command_with_retry(0x04, "cell_voltages")
        
        if cell_result.status == ResponseStatus.SUCCESS:
            cell_voltages = self._parse_cell_voltages(cell_result.response)
            if cell_voltages:
                bms_data.cell_voltages = cell_voltages
        
        return bms_data
    
    async def diagnose(self) -> Dict[str, Any]:
        """
        Modo diagnóstico - Prueba todos los comandos y reporta resultados
        """
        results = {
            'connection': False,
            'uuids_detected': False,
            'protocol': None,
            'commands_tested': [],
            'working_commands': [],
            'recommendations': []
        }
        
        if not self.client or not self.client.is_connected:
            results['recommendations'].append("No hay conexión al BMS")
            return results
        
        results['connection'] = True
        results['uuids_detected'] = bool(self.write_uuid and self.notify_uuid)
        results['protocol'] = self.detected_protocol.value
        
        print("\n" + "="*60)
        print("          MODO DIAGNÓSTICO - BMS ROBUST")
        print("="*60)
        
        # Probar wake-up
        print("\n[1] Probando comandos de wake-up...")
        await self.wake_up_bms()
        
        # Probar todos los formatos de comando
        print("\n[2] Probando formatos de comando para registro 0x03...")
        variants = self._get_all_command_variants(0x03)
        
        for name, cmd in variants:
            print(f"\n  Probando: {name}")
            print(f"    Comando: {cmd.hex()}")
            
            result = await self._send_single_command(cmd, name, self.base_timeout)
            
            test_result = {
                'name': name,
                'command': cmd.hex(),
                'status': result.status.value,
                'latency_ms': result.latency_ms
            }
            
            if result.status == ResponseStatus.SUCCESS:
                print(f"    ✓ ÉXITO - {len(result.response)} bytes en {result.latency_ms:.1f}ms")
                print(f"    Respuesta: {result.response.hex()[:60]}...")
                test_result['response_preview'] = result.response.hex()[:60]
                results['working_commands'].append(name)
            elif result.status == ResponseStatus.TIMEOUT:
                print(f"    ✗ TIMEOUT después de {result.latency_ms:.1f}ms")
            elif result.status == ResponseStatus.EMPTY:
                print(f"    ✗ RESPUESTA VACÍA")
            else:
                print(f"    ✗ ERROR: {result.error_message}")
            
            results['commands_tested'].append(test_result)
            await asyncio.sleep(0.5)
        
        # Generar recomendaciones
        print("\n" + "="*60)
        print("RECOMENDACIONES")
        print("="*60)
        
        if results['working_commands']:
            best = results['working_commands'][0]
            print(f"\n✓ Protocolo recomendado: {best}")
            results['recommendations'].append(f"Usar protocolo: {best}")
        else:
            print("\n✗ Ningún comando funcionó. Posibles causas:")
            print("  - El BMS está en modo de bajo consumo profundo")
            print("  - El BMS usa un protocolo no estándar")
            print("  - Hay un problema de emparejamiento BLE")
            print("  - El BMS requiere autenticación previa")
            
            results['recommendations'].extend([
                "Verificar que el BMS esté encendido",
                "Intentar reconectar después de un ciclo de encendido",
                "Probar con bms_sniffer.py para ver si envía datos automáticamente",
                "Verificar si el BMS requiere PIN de emparejamiento"
            ])
        
        return results
    
    def print_stats(self):
        """Imprimir estadísticas de la sesión"""
        print("\n" + "="*40)
        print("ESTADÍSTICAS DE LA SESIÓN")
        print("="*40)
        print(f"  Comandos enviados:      {self.stats['commands_sent']}")
        print(f"  Respuestas recibidas:   {self.stats['responses_received']}")
        print(f"  Timeouts:               {self.stats['timeouts']}")
        print(f"  Respuestas vacías:      {self.stats['empty_responses']}")
        print(f"  Lecturas exitosas:      {self.stats['successful_reads']}")
        
        if self.stats['commands_sent'] > 0:
            success_rate = (self.stats['successful_reads'] / self.stats['commands_sent']) * 100
            print(f"  Tasa de éxito:          {success_rate:.1f}%")


async def main():
    parser = argparse.ArgumentParser(
        description='BMS Robust Connector - Solución a respuestas vacías',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ejemplos:
  python bms_robust.py --mac XX:XX:XX:XX:XX:XX
  python bms_robust.py --mac XX:XX:XX:XX:XX:XX --diagnose
  python bms_robust.py --mac XX:XX:XX:XX:XX:XX --retries 10 --timeout 5
  python bms_robust.py --mac XX:XX:XX:XX:XX:XX --verbose --continuous
        """
    )
    parser.add_argument('--mac', '-m', required=True, help='Dirección MAC del BMS')
    parser.add_argument('--diagnose', '-d', action='store_true', help='Modo diagnóstico completo')
    parser.add_argument('--retries', '-r', type=int, default=5, help='Máximo de reintentos (default: 5)')
    parser.add_argument('--timeout', '-t', type=float, default=3.0, help='Timeout base en segundos (default: 3.0)')
    parser.add_argument('--verbose', '-v', action='store_true', help='Modo verbose')
    parser.add_argument('--continuous', '-c', action='store_true', help='Lectura continua')
    parser.add_argument('--interval', '-i', type=float, default=5.0, help='Intervalo de lectura continua (default: 5.0)')
    parser.add_argument('--scan', '-s', action='store_true', help='Escanear dispositivos BLE')
    
    args = parser.parse_args()
    
    # Modo escaneo
    if args.scan:
        print("Escaneando dispositivos BLE...")
        devices = await BleakScanner.discover(timeout=5.0)
        print(f"\nDispositivos encontrados: {len(devices)}")
        for d in devices:
            name = d.name or "Unknown"
            print(f"  {name} [{d.address}] RSSI: {d.rssi}")
        return
    
    connector = BMSRobustConnector(
        mac_address=args.mac,
        max_retries=args.retries,
        base_timeout=args.timeout,
        verbose=args.verbose
    )
    
    try:
        if not await connector.connect():
            print("\n✗ Error: No se pudo conectar al BMS")
            print("\nSugerencias:")
            print("  1. Verifica que el BMS esté encendido")
            print("  2. Verifica la dirección MAC")
            print("  3. Asegúrate de estar cerca del dispositivo")
            print("  4. Prueba: python bms_robust.py --scan")
            return
        
        if args.diagnose:
            # Modo diagnóstico
            await connector.diagnose()
        elif args.continuous:
            # Lectura continua
            print(f"\nLectura continua cada {args.interval} segundos (Ctrl+C para detener)")
            try:
                while True:
                    data = await connector.read_bms_data()
                    if data:
                        print(data)
                    else:
                        print(f"[{datetime.now().strftime('%H:%M:%S')}] Sin datos")
                    await asyncio.sleep(args.interval)
            except KeyboardInterrupt:
                print("\nDetenido por el usuario")
        else:
            # Lectura única
            print("\nLeyendo datos del BMS...")
            data = await connector.read_bms_data()
            
            if data:
                print(data)
            else:
                print("\n✗ No se pudieron obtener datos del BMS")
                print("\nPrueba el modo diagnóstico para más información:")
                print(f"  python bms_robust.py --mac {args.mac} --diagnose")
        
        connector.print_stats()
        
    except KeyboardInterrupt:
        print("\n\nInterrumpido por el usuario")
    finally:
        await connector.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
