"""Entry point of the packaged program (PyInstaller): runs fnirsi_companion/__main__.py as the main module,
the same code `python -m fnirsi_companion` runs."""
import runpy

runpy.run_module('fnirsi_companion', run_name='__main__', alter_sys=True)
