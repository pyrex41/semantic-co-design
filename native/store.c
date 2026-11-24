// SQLite handler for the keyed-state effect. The generated unit defines step and initial.
#include <sqlite3.h>
#include <stdint.h>
#include <stdlib.h>

static int transition(sqlite3 *db, sqlite3_int64 id, const char *event) {
    sqlite3_stmt *query = NULL, *write = NULL;
    const char *next = NULL, *observation = NULL;
    int ok = 0;
    if (sqlite3_exec(db, "BEGIN IMMEDIATE", NULL, NULL, NULL) != SQLITE_OK) goto done;
    if (sqlite3_prepare_v2(db, "SELECT state FROM instances WHERE id=?", -1, &query, NULL) != SQLITE_OK ||
        sqlite3_bind_int64(query, 1, id) != SQLITE_OK) goto done;
    int rc = sqlite3_step(query);
    if (rc != SQLITE_ROW && rc != SQLITE_DONE) goto done;
    const char *state = rc == SQLITE_DONE ? initial : (const char *)sqlite3_column_text(query, 0);
    if (!state || !step(state, event, &next, &observation)) {
        fputs("unknown event or stored state\n", stderr);
        goto done;
    }
    sqlite3_finalize(query);
    query = NULL;
    if (sqlite3_prepare_v2(db, "INSERT INTO instances(id,state) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state",
                           -1, &write, NULL) != SQLITE_OK ||
        sqlite3_bind_int64(write, 1, id) != SQLITE_OK ||
        sqlite3_bind_text(write, 2, next, -1, SQLITE_STATIC) != SQLITE_OK ||
        sqlite3_step(write) != SQLITE_DONE) goto done;
    if (sqlite3_exec(db, "COMMIT", NULL, NULL, NULL) != SQLITE_OK) goto done;
    printf("%s %s\n", next, observation);
    ok = 1;
done:
    if (!ok) {
        fprintf(stderr, "transition failed: %s\n", sqlite3_errmsg(db));
        sqlite3_exec(db, "ROLLBACK", NULL, NULL, NULL);
    }
    sqlite3_finalize(query);
    sqlite3_finalize(write);
    return ok;
}

int main(int argc, char **argv) {
    if (argc < 4 || !*argv[2] || strspn(argv[2], "0123456789") != strlen(argv[2])) {
        fputs("usage: program DATABASE UINT32_ID EVENT...\n", stderr);
        return 2;
    }
    unsigned long long id = strtoull(argv[2], NULL, 10);
    if (id > UINT32_MAX) return 2;
    sqlite3 *db = NULL;
    int ok = sqlite3_open(argv[1], &db) == SQLITE_OK;
    if (ok) {
        sqlite3_busy_timeout(db, 5000);
        ok = sqlite3_exec(db, "CREATE TABLE IF NOT EXISTS instances(id INTEGER PRIMARY KEY,state TEXT NOT NULL)",
                          NULL, NULL, NULL) == SQLITE_OK;
    }
    for (int i = 3; ok && i < argc; i++) ok = transition(db, id, argv[i]);
    sqlite3_close(db);
    return ok ? 0 : 1;
}
