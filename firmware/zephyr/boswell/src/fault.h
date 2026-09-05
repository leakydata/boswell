#ifndef BOSWELL_FAULT_H
#define BOSWELL_FAULT_H

#include <stdbool.h>
#include <stdint.h>

/* What the last fatal error was, carried across the reset that followed it.
 *
 * The console is USB CDC, and USB stops being serviced the instant the CPU
 * halts -- so a panic banner is written into a buffer that nothing will ever
 * drain, and the board reboots looking like a bare watchdog timeout with no
 * explanation. This records the same facts into RAM the reset does not
 * clear, so the next boot can say what actually happened. */
struct fault_record {
    uint32_t magic;
    uint32_t reason;      /* K_ERR_* */
    uint32_t pc;
    uint32_t lr;
    uint32_t psr;
    uint32_t uptime_ms;
    uint32_t stack_used;  /* of the faulting thread, 0 if unknown */
    uint32_t stack_size;
    char     thread[20];
    uint32_t repeats;     /* how many boots in a row ended this way */
};

/* True if a fatal error was recorded before the last reset. */
bool fault_last(struct fault_record *out);

/* Forget it, so the next boot does not report the same crash again. */
void fault_clear(void);

/* Human-readable K_ERR_* name. */
const char *fault_reason_name(uint32_t reason);

#endif /* BOSWELL_FAULT_H */
