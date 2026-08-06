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

void _start(void) {
    for (;;) {
        counter = outer(counter);
    }
}
