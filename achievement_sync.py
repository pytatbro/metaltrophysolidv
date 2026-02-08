import time
import configparser
import struct
import json
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

try:
    from winsdk.windows.ui.notifications import ToastNotificationManager, ToastNotification
    from winsdk.windows.data.xml.dom import XmlDocument
    WINRT_AVAILABLE = True
except ImportError:
    WINRT_AVAILABLE = False
    print("Warning: winsdk not available. Install with: pip install winsdk")


class IniFileHandler(FileSystemEventHandler):
    """Handler for monitoring changes to stats.ini"""
    
    def __init__(self, source_file, target_file, achievements_json_file=None):
        self.source_file = Path(source_file).resolve()
        self.target_file = Path(target_file).resolve()
        self.achievements_json_file = Path(achievements_json_file).resolve() if achievements_json_file else None
        self.last_modified = 0
        self.known_trophies = set()  # Track trophies we've already seen
        self.achievements_data = {}  # Store achievement metadata
        
        # Load achievements metadata
        if self.achievements_json_file and self.achievements_json_file.exists():
            self.load_achievements_metadata()
        
        # Initialize known trophies from existing achievements.ini
        if self.target_file.exists():
            self.load_existing_trophies()
        
    def on_modified(self, event):
        """Triggered when the watched file is modified"""
        self._handle_change(event.src_path)
    
    def on_created(self, event):
        """Some editors create a new file when saving"""
        self._handle_change(event.src_path)
    
    def on_moved(self, event):
        """Some editors use atomic write (save to temp, then move)"""
        if hasattr(event, 'dest_path'):
            self._handle_change(event.dest_path)
    
    def _handle_change(self, changed_path):
        """Handle file change events"""
        # Convert to absolute path for comparison
        event_path = Path(changed_path).resolve()
        
        if event_path == self.source_file:
            # Debounce: ignore rapid successive modifications
            current_time = time.time()
            if current_time - self.last_modified < 0.5:
                return
            self.last_modified = current_time
            
            print(f"[{time.strftime('%H:%M:%S')}] Detected change in {self.source_file.name}")
            self.sync_achievements()
    
    def load_achievements_metadata(self):
        """Load achievement metadata from JSON file"""
        try:
            with open(self.achievements_json_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                # Convert list to dict keyed by trophy name
                for achievement in data:
                    self.achievements_data[achievement['name']] = achievement
                print(f"Loaded metadata for {len(self.achievements_data)} achievements")
        except Exception as e:
            print(f"Warning: Could not load achievements metadata: {e}")
    
    def load_existing_trophies(self):
        """Load existing trophies from achievements.ini to track what we already have"""
        try:
            config = configparser.ConfigParser()
            config.read(self.target_file)
            
            if config.has_section('SteamAchievements'):
                count = config.getint('SteamAchievements', 'Count', fallback=0)
                for i in range(count):
                    trophy_name = config.get('SteamAchievements', f'{i:05d}', fallback=None)
                    if trophy_name:
                        self.known_trophies.add(trophy_name)
                print(f"Loaded {len(self.known_trophies)} existing trophies")
        except Exception as e:
            print(f"Warning: Could not load existing trophies: {e}")
    
    def send_toast_notification(self, trophy_name):
        """Send a Windows toast notification for a new achievement"""
        if not WINRT_AVAILABLE:
            print(f"[NOTIFICATION] New trophy: {trophy_name}")
            return
        
        try:
            # Get achievement metadata
            achievement = self.achievements_data.get(trophy_name, {})
            display_name = achievement.get('displayName', trophy_name)
            description = achievement.get('description', 'Achievement unlocked!')
            icon_path = achievement.get('icon', '')
            
            # Convert relative icon path to absolute path
            if icon_path and not Path(icon_path).is_absolute():
                # Assume icon path is relative to the achievements JSON file
                icon_path = (self.achievements_json_file.parent / icon_path).resolve()
            else:
                icon_path = Path(icon_path).resolve() if icon_path else None
            
            # Create toast XML
            toast_xml = f'''
            <toast>
                <visual>
                    <binding template="ToastGeneric">
                        <text>{display_name}</text>
                        <text>{description}</text>
                        {f'<image placement="appLogoOverride" src="file:///{icon_path}"/>' if icon_path and icon_path.exists() else ''}
                    </binding>
                </visual>
            </toast>
            '''
            
            # Create XML document
            xml_doc = XmlDocument()
            xml_doc.load_xml(toast_xml)
            
            # Create and show toast
            toast = ToastNotification(xml_doc)
            notifier = ToastNotificationManager.create_toast_notifier("Microsoft.XboxGamingOverlay_8wekyb3d8bbwe!App")
            notifier.show(toast)
            
            print(f"✓ Notification sent for: {display_name}")
            
        except Exception as e:
            print(f"Error sending notification: {e}")
    
    def parse_time_value(self, time_str):
        """
        Convert hex time string to Unix timestamp
        Example: B0BA866959 -> first 8 hex chars -> little-endian conversion
        Returns None if parsing fails
        """
        try:
            # Take first 8 hex characters
            hex_time = time_str[:8]
            # Convert to bytes (treating as big-endian hex string)
            time_bytes = bytes.fromhex(hex_time)
            # Unpack as little-endian 32-bit unsigned integer
            unix_time = struct.unpack('<I', time_bytes)[0]
            return unix_time
        except (ValueError, struct.error) as e:
            print(f"Error parsing time value '{time_str}': {e}")
            return None
    
    def read_stat_ini(self):
        """Read and parse stats.ini file"""
        config = configparser.ConfigParser()
        config.read(self.source_file)
        
        trophies = {}
        for section in config.sections():
            if section.startswith('Trophy_'):
                # Check if both State and Time exist
                if not config.has_option(section, 'State') or not config.has_option(section, 'Time'):
                    print(f"Skipping {section}: missing State or Time")
                    continue
                
                state = config.get(section, 'State')
                time_hex = config.get(section, 'Time')
                
                # Validate that State and Time are not empty
                if not state or not time_hex:
                    print(f"Skipping {section}: empty State or Time")
                    continue
                
                # Validate that Time has at least 8 hex characters
                if len(time_hex) < 8:
                    print(f"Skipping {section}: Time value too short ({time_hex})")
                    continue
                
                # Parse state (assuming first 2 chars indicate achievement status)
                achieved = 1 if state[:2] == '01' else 0
                
                # Parse time
                unlock_time = self.parse_time_value(time_hex)
                
                # If time parsing failed (returned None due to error), skip this trophy
                if unlock_time is None:
                    print(f"Skipping {section}: failed to parse Time value")
                    continue
                
                trophies[section] = {
                    'Achieved': achieved,
                    'UnlockTime': unlock_time
                }
        
        return trophies
    
    def sync_achievements(self):
        """Sync trophies from stats.ini to achievements.ini"""
        try:
            # Read source trophies
            source_trophies = self.read_stat_ini()
            
            if not source_trophies:
                print("No trophies found in stats.ini")
                return
            
            # Detect new trophies (not in known_trophies set)
            new_trophies = []
            for trophy_name in source_trophies.keys():
                if trophy_name not in self.known_trophies:
                    new_trophies.append(trophy_name)
                    self.known_trophies.add(trophy_name)
            
            # Read existing achievements.ini to preserve any extra sections
            target_config = configparser.ConfigParser()
            if self.target_file.exists():
                target_config.read(self.target_file)
            
            # Prepare trophy list
            trophy_list = list(source_trophies.keys())
            
            # Update or create trophy sections in config
            for trophy_name, trophy_data in source_trophies.items():
                if not target_config.has_section(trophy_name):
                    target_config.add_section(trophy_name)
                
                target_config.set(trophy_name, 'Achieved', str(trophy_data['Achieved']))
                target_config.set(trophy_name, 'CurProgress', '0')
                target_config.set(trophy_name, 'MaxProgress', '0')
                target_config.set(trophy_name, 'UnlockTime', str(trophy_data['UnlockTime']))
            
            # Manually write the file with correct ordering
            with open(self.target_file, 'w') as f:
                # Write SteamAchievements section first
                f.write('[SteamAchievements]\n')
                
                # Write trophy entries (00000, 00001, etc.) BEFORE Count
                for idx, trophy_name in enumerate(trophy_list):
                    f.write(f'{idx:05d}={trophy_name}\n')
                
                # Write Count last
                f.write(f'Count={len(trophy_list)}\n')
                f.write('\n')
                
                # Write all trophy sections
                for trophy_name in trophy_list:
                    f.write(f'[{trophy_name}]\n')
                    f.write(f'Achieved={target_config.get(trophy_name, "Achieved")}\n')
                    f.write(f'CurProgress={target_config.get(trophy_name, "CurProgress")}\n')
                    f.write(f'MaxProgress={target_config.get(trophy_name, "MaxProgress")}\n')
                    f.write(f'UnlockTime={target_config.get(trophy_name, "UnlockTime")}\n')
                    f.write('\n')
            
            print(f"✓ Updated {self.target_file.name} with {len(source_trophies)} trophies")
            
            # Send notifications for new trophies
            for trophy_name in new_trophies:
                print(f"🏆 New achievement unlocked: {trophy_name}")
                self.send_toast_notification(trophy_name)
            
        except Exception as e:
            print(f"Error syncing achievements: {e}")


def main():
    # File paths - adjust these to your actual paths
    source_file = "stats.ini"
    target_file = "achievements.ini"
    achievements_json = "achievements.json"  # Path to achievements metadata JSON
    
    print("=" * 60)
    print("Achievement Sync Watcher")
    print("=" * 60)
    print(f"Monitoring: {Path(source_file).resolve()}")
    print(f"Target: {Path(target_file).resolve()}")
    print(f"Metadata: {Path(achievements_json).resolve()}")
    print("Press Ctrl+C to stop...\n")
    
    # Create handler and observer
    event_handler = IniFileHandler(source_file, target_file, achievements_json)
    observer = Observer()
    
    # Watch the directory containing the source file
    watch_path = Path(source_file).parent.resolve()
    if watch_path == Path('.').resolve():
        watch_path = Path.cwd()
    
    observer.schedule(event_handler, str(watch_path), recursive=False)
    
    # Do an initial sync
    print("Performing initial sync...")
    event_handler.sync_achievements()
    print()
    print()
    # Start watching
    observer.start()
    
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping watcher...")
        observer.stop()
    
    observer.join()
    print("Watcher stopped.")


if __name__ == "__main__":
    main()