#!/bin/bash
# Script para hacer pairing con el BMS usando bluetoothctl
# Uso: ./pair_bms.sh <MAC_ADDRESS> [PIN]

MAC="${1:-}"
PIN="${2:-123456}"

if [ -z "$MAC" ]; then
    echo "Uso: $0 <MAC_ADDRESS> [PIN]"
    echo "Ejemplo: $0 AA:BB:CC:DD:EE:FF 123456"
    echo ""
    echo "Primero escanea para encontrar la MAC:"
    echo "  bluetoothctl scan on"
    exit 1
fi

echo "=== Pairing con BMS ==="
echo "MAC: $MAC"
echo "PIN: $PIN"
echo ""

# Crear script para bluetoothctl
bluetoothctl << EOF
power on
agent on
default-agent
scan off
pair $MAC
$PIN
trust $MAC
connect $MAC
EOF

echo ""
echo "Si el pairing fue exitoso, ahora puedes ejecutar:"
echo "  python3 smart_bts_protocol.py"
