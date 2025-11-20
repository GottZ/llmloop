import time
import psycopg2
import psycopg2.extras
import json
from config import Config

def get_db_connection():
    if not Config.DATABASE_URL:
        raise ValueError("DATABASE_URL is not set")
    conn = psycopg2.connect(Config.DATABASE_URL)
    conn.autocommit = True
    return conn

def wait_for_db(retries=30, delay=2):
    for i in range(retries):
        try:
            conn = get_db_connection()
            conn.close()
            print("Database connection successful.")
            return
        except psycopg2.OperationalError:
            print(f"Waiting for database... ({i+1}/{retries})")
            time.sleep(delay)
    raise Exception("Could not connect to database")

def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    
    # Schema Version
    cur.execute("""
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY,
            applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    
    # Check version
    cur.execute("SELECT MAX(version) FROM schema_version")
    row = cur.fetchone()
    current_version = row[0] if row and row[0] is not None else 0
    
    # Migration 1: Initial Schema
    if current_version < 1:
        print("Applying migration 1...")
        cur.execute("""
            CREATE TABLE threads (
                id SERIAL PRIMARY KEY,
                cwd TEXT,
                context_summary TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            
            CREATE TABLE messages (
                id SERIAL PRIMARY KEY,
                thread_id INTEGER REFERENCES threads(id) ON DELETE CASCADE,
                role TEXT NOT NULL, -- user, planner, tool_runner, system
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            
            CREATE TABLE tool_runs (
                id SERIAL PRIMARY KEY,
                thread_id INTEGER REFERENCES threads(id) ON DELETE CASCADE,
                command TEXT NOT NULL,
                output_stdout TEXT,
                output_stderr TEXT,
                exit_code INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            
            CREATE TABLE llm_backends (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                base_url TEXT NOT NULL,
                api_key TEXT NOT NULL,
                is_active BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            
            CREATE TABLE model_configs (
                id SERIAL PRIMARY KEY,
                backend_id INTEGER REFERENCES llm_backends(id) ON DELETE CASCADE,
                role TEXT NOT NULL, -- planner, tool_runner
                model_name TEXT NOT NULL,
                parameters JSONB DEFAULT '{}'::jsonb,
                UNIQUE(backend_id, role)
            );
        """)
        cur.execute("INSERT INTO schema_version (version) VALUES (1)")
        
    # Seeding
    cur.execute("SELECT COUNT(*) FROM llm_backends")
    if cur.fetchone()[0] == 0 and Config.SEED_OPENAI_API_KEY:
        print("Seeding initial LLM backend from environment...")
        cur.execute("""
            INSERT INTO llm_backends (name, base_url, api_key, is_active)
            VALUES (%s, %s, %s, %s)
            RETURNING id
        """, ("Initial Seed", Config.SEED_OPENAI_BASE_URL, Config.SEED_OPENAI_API_KEY, True))
        backend_id = cur.fetchone()[0]
        
        # Seed default configs
        default_params = json.dumps({"temperature": 0.7, "max_tokens": 1000, "top_p": 0.9})
        cur.execute("""
            INSERT INTO model_configs (backend_id, role, model_name, parameters)
            VALUES 
            (%s, 'planner', 'gpt-4o-mini', %s),
            (%s, 'tool_runner', 'gpt-4o-mini', %s)
        """, (backend_id, default_params, backend_id, default_params))

    cur.close()
    conn.close()
    print("Database initialized.")

def get_active_backend():
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM llm_backends WHERE is_active = TRUE LIMIT 1")
    return cur.fetchone()

def get_model_config(backend_id, role):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM model_configs WHERE backend_id = %s AND role = %s", (backend_id, role))
    return cur.fetchone()
