import sqlite3, os
from passlib.context import CryptContext
ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
conn = sqlite3.connect(os.getenv("DB_PATH", "/data/opal.db"))
conn.execute("UPDATE users SET password_hash=?, must_change_password=1 WHERE username='admin'",
(ctx.hash("TempPass123"),))
conn.commit()
print("Done. Login with admin / TempPass123")
