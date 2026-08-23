import sqlite3

def init_db(DB_PATH):
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    # Guild Table (guild_id, guild_name)
    cursor.execute('''CREATE TABLE IF NOT EXISTS guilds (guild_id INTEGER PRIMARY KEY,guild_name TEXT NOT NULL)''')
    # Config Table (guild_id, config_key, config_value)
    cursor.execute('''CREATE TABLE IF NOT EXISTS config (guild_id INTEGER,config_key TEXT NOT NULL,config_value TEXT,PRIMARY KEY (guild_id, config_key),FOREIGN KEY (guild_id) REFERENCES guilds(guild_id))''')
    conn.commit()
    conn.close()

def save_guild(DB_PATH, guild_id, guild_name):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''INSERT INTO guilds (guild_id, guild_name)VALUES (?, ?)ON CONFLICT(guild_id) DO UPDATE SET guild_name = excluded.guild_name''', (guild_id, guild_name))
    conn.commit()
    conn.close()

def set_config(DB_PATH, guild_id, key, value):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''INSERT INTO config (guild_id, config_key, config_value)VALUES (?, ?, ?)ON CONFLICT(guild_id, config_key) DO UPDATE SET config_value = excluded.config_value''', (guild_id, key, str(value)))
    conn.commit()
    conn.close()

def get_config(DB_PATH, guild_id, key, default=None):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''SELECT config_value FROM config WHERE guild_id = ? AND config_key = ?''', (guild_id, key))
    row = cursor.fetchone()
    conn.close()

    return row[0] if row else default

def get_all_config(DB_PATH, guild_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''SELECT config_key, config_value FROM config WHERE guild_id = ?''', (guild_id,))
    rows = cursor.fetchall()
    conn.close()
    return rows

def delete_config(DB_PATH, guild_id, key):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''DELETE FROM config WHERE guild_id = ? AND config_key = ?''', (guild_id, key))
    conn.commit()
    conn.close()
