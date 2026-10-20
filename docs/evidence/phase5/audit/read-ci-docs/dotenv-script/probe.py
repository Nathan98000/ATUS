import atus_pipeline.config as c
print("ATUS_RELEASE seen via script entrypoint with .env in CWD:", c.load_settings().release)
