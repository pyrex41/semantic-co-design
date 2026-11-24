// Run the specialized representative transitions and restore the actual fields.
static int decode(const char *term, char *symbol, const char **field, const char *stored) {
    const char *dash = strchr(term, '-');
    size_t n = dash ? (size_t)(dash - term) : strlen(term);
    if (n + 3 > TERM_SIZE) return 0;
    memcpy(symbol, term, n);
    symbol[n] = 0;
    *field = dash ? dash + 1 : NULL;
    if (!dash) return 1;
    if (strlen(*field) != 64 || strspn(*field,"0123456789abcdef") != 64) return 0;
    strcat(symbol, !stored || strcmp(stored,*field) ? "-b" : "-a");
    return 1;
}

static void resolve(const char *symbol, const char *stored, const char *incoming, char *result) {
    const char *dash = strchr(symbol, '-');
    if (!dash) { strcpy(result, symbol); return; }
    snprintf(result, TERM_SIZE, "%.*s-%s", (int)(dash-symbol), symbol,
             dash[1]=='a' ? stored : incoming);
}

static int step(const char *state,const char *event,const char **next,const char **out) {
    char s[TERM_SIZE], e[TERM_SIZE], n[TERM_SIZE], o[TERM_SIZE];
    static char next_text[TERM_SIZE], out_text[TERM_SIZE];
    const char *stored, *incoming, *target, *observation;
    if (!decode(state,s,&stored,NULL) || !decode(event,e,&incoming,stored)) return 0;
    if (stored) s[strlen(s)-1]='a';
    if (!symbol_step(s,e,&target,&observation)) return 0;
    resolve(target,stored,incoming,n);
    resolve(observation,stored,incoming,o);
    strcpy(next_text,n);
    strcpy(out_text,o);
    *next=next_text;
    *out=out_text;
    return 1;
}
