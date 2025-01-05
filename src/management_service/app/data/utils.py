
from app.data.models import Base

    



def run_migrations(engine):
    Base.metadata.create_all(bind=engine)

if __name__ == "__main__":
    run_migrations()
