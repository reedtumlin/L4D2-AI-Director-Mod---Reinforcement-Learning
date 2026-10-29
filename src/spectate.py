import subprocess
import os
import time

def launch_spectator():
    print("[Spectator] Preparing to launch Left 4 Dead 2...")
    
    # --- CONFIGURATION ---
    # Your exact L4D2 game folder
    game_dir = r"C:\Program Files (x86)\Steam\steamapps\common\Left 4 Dead 2"
    exe_path = os.path.join(game_dir, "left4dead2.exe")
    
    # The IP address of your machine (Not 127.0.0.1, which can fail to connect)
    # Based on your earlier logs, this is your local IP:
    server_ip = "192.168.0.157" 
    server_port = "27015"
    
    if not os.path.exists(exe_path):
        print(f"[Error] Could not find left4dead2.exe at: {exe_path}")
        print("Please check your Steam installation path.")
        return
        
    # Arguments to launch the game, skip the intro video, open the console, and connect
    args = [
        "-insecure",      # Required to connect to your dedicated server
        "-console",       # Opens the dev console automatically
        "-novid",         # Skips the intro video
        "+connect", f"{server_ip}:{server_port}"
    ]
    
    try:
        print(f"[Spectator] Launching: {exe_path}")
        print(f"[Spectator] Connecting to: {server_ip}:{server_port}")
        
        # Launch the game
        subprocess.Popen([exe_path] + args, cwd=game_dir)
        
        print("\n[Spectator] Game launched successfully!")
        print("[Spectator] 1. Click 'OK' on the VAC warning popup.")
        print("[Spectator] 2. Wait for the map to load.")
        print("[Spectator] 3. Open the console (~) and type: jointeam 1")
        print("[Spectator] 4. Then type: nb_debug PATH")
        
    except Exception as e:
        print(f"[Error] Failed to launch: {e}")

if __name__ == "__main__":
    launch_spectator()