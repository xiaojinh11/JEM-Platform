import sys
sys.path.insert(0, r"d:\zidongh\backend")
from app.db.session import SessionLocal
from app.models.user import User
from app.core.security import hash_password

db = SessionLocal()
admin = db.query(User).filter_by(username="admin").first()
if not admin:
    print("admin not found")
    sys.exit(1)
original = admin.password_hash
admin.password_hash = hash_password("test1234")
db.commit()
print(f"CHANGED admin password -> 'test1234'")
print(f"original_hash={original}")
with open(r"d:\zidongh\backend\data\.test_admin_hash.tmp", "w") as f:
    f.write(original)
