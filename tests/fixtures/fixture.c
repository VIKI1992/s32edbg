/* The symbol-layer test fixture: a tiny program shaped like the real app --
 * T32, linked into the RTU0 CodeRAM window, nested calls for the unwinder,
 * a global for watch tests. Built once and committed (see README.md here);
 * the tests read the ELF, they never run it.
 */

int counter;

static int leaf(int x) {
    counter += x;
    return x * 2;
}

static int middle(int x) {
    int y = leaf(x + 1);
    return y + leaf(x);
}

int outer(int x) {
    return middle(x) + 1;
}

/* Shapes for the variables layer: enum, nested struct, array, pointers,
 * char*, volatile, float, file statics and a function-local static. */

enum mode { MODE_IDLE, MODE_RUN, MODE_FAULT };

struct limits { int min; int max; };

struct config {
    enum mode mode;
    unsigned char flags;
    struct limits limits[3];
    const char *name;
    struct config *next;
};

volatile unsigned int ticks;
float ratio = 1.5f;
static char greeting[] = "hello";
static struct config cfg = { MODE_RUN, 0x5A, {{0, 10}, {1, 20}, {2, 30}}, greeting, 0 };

static int bump(int delta) {
    static int calls;
    struct limits *lp = &cfg.limits[1];
    calls += 1;
    return lp->max + delta + (int)ticks;
}

void _start(void) {
    for (;;) {
        counter = outer(counter) + bump(1);
    }
}

/* Stack-monitor shapes: painted-stack arrays that the ARXML fixture
 * (stacks_os_ecuc.arxml) references by name via OsCfg_Stack_<name>_Dyn.
 * Appended after _start so every earlier line number stays put. */
unsigned int OsCfg_Stack_TestA_Dyn[64];
unsigned int OsCfg_Stack_TestB_Dyn[32];
