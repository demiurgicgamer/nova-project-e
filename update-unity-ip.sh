#!/bin/bash
# update-unity-ip.sh
# Run this whenever your LAN IP changes to patch both Unity files automatically.
# Usage: bash update-unity-ip.sh

UNITY_PROJECT="D:/MyWorks/Unity/Project-E"
WS_FILE="$UNITY_PROJECT/Assets/_App/Scripts/Voice/WebSocketManager.cs"
API_FILE="$UNITY_PROJECT/Assets/_App/Scripts/Data/APIService.cs"

# Get current LAN IP (first 192.168.x.x address)
NEW_IP=$(ipconfig 2>/dev/null | grep "IPv4" | grep "192.168" | head -1 | awk '{print $NF}' | tr -d '\r')

if [ -z "$NEW_IP" ]; then
    echo "ERROR: Could not detect LAN IP. Are you connected to Wi-Fi?"
    exit 1
fi

echo "Detected IP: $NEW_IP"

# Patch WebSocketManager.cs
sed -i "s|ws://[0-9.]*:3000|ws://$NEW_IP:3000|g" "$WS_FILE"
echo "Updated WebSocketManager.cs"

# Patch APIService.cs
sed -i "s|http://[0-9.]*:3000|http://$NEW_IP:3000|g" "$API_FILE"
echo "Updated APIService.cs"

# Patch all scene files (Unity bakes SerializeField values into .unity files)
echo "Patching scene files..."
find "$UNITY_PROJECT/Assets/_App/Scenes" -name "*.unity" | while read scene; do
    sed -i "s|ws://[0-9.]*:3000|ws://$NEW_IP:3000|g" "$scene"
    sed -i "s|http://[0-9.]*:3000|http://$NEW_IP:3000|g" "$scene"
    echo "  Patched: $scene"
done

echo "Done. All files now point to $NEW_IP:3000"
