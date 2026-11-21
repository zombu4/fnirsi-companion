"""Link layer common to both meters.

frames: frame codec and parser of the USB serial control channel.
port: SerialLink, one worker thread per open port.
discover: serial ports of the meters, update drives, hot-plug watcher.

The submodules are imported by name; this package imports none of them, so that the frame codec
can be used without Qt.
"""
