import sys
import os

# Before anything else: the origin every other timestamp is measured from.
sys.path.insert(0, os.path.join(os.path.abspath(os.path.dirname(__file__)), "plugin"))
from tracing import mark_process_start

mark_process_start()


def main():
    try:
        # Get the absolute path of the plugin directory
        plugindir = os.path.abspath(os.path.dirname(__file__))
        os.chdir(plugindir)

        # Add necessary directories to the Python path
        directories = [
            plugindir,
            os.path.join(plugindir, "lib"),
            os.path.join(plugindir, "plugin")
        ]
        for directory in directories:
            if os.path.exists(directory):
                sys.path.append(directory)
            else:
                print(f"Warning: Directory not found: {directory}", file=sys.stderr)

        # Import and run the BrowserHistory plugin
        from plugin.main import BrowserHistory
        BrowserHistory().run()

    except ImportError as e:
        print(f"Error: Failed to import required modules. {e}", file=sys.stderr)
    except Exception as e:
        print(f"An unexpected error occurred: {e}", file=sys.stderr)

if __name__ == "__main__":
    main()