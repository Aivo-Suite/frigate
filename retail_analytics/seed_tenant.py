import bcrypt
import uuid
from sqlalchemy import text
from db_config import engine, init_db

def create_tenant(name, username, password):
    init_db()
    
    tenant_id = str(uuid.uuid4())
    api_key = str(uuid.uuid4()).replace('-', '')
    
    # Hash password
    salt = bcrypt.gensalt()
    password_hash = bcrypt.hashpw(password.encode(), salt).decode()
    
    try:
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO tenants (tenant_id, name, username, password_hash, api_key) VALUES (:tid, :n, :u, :p, :k)"),
                {"tid": tenant_id, "n": name, "u": username, "p": password_hash, "k": api_key}
            )
        print(f"✅ Cliente '{name}' criado com sucesso!")
        print(f"  🏢 Tenant ID: {tenant_id}")
        print(f"  👤 Usuário: {username}")
        print(f"  🔑 API Key: {api_key}")
        print(f"\nATENÇÃO: Configure a variável TENANT_API_KEY no Frigate (Edge) deste cliente com a API Key acima.")
    except Exception as e:
        print(f"❌ Erro ao criar cliente: {e}")

if __name__ == "__main__":
    create_tenant("Loja Matriz - Aivo", "admin", "admin123")
