#include "fault.h"

#include <zephyr/kernel.h>
#include <zephyr/fatal.h>
#include <zephyr/sys/reboot.h>
#include <string.h>

#define FAULT_MAGIC 0xB05EFA17u

/* A fault sooner than this after boot, following a boot that also faulted,
 * counts as the same fault repeating rather than a new one. */
#define FAULT_LOOP_MS 30000

/* __noinit keeps this out of .bss, so the boot code does not zero it. The
 * nRF52 does not clear RAM on a soft or watchdog reset, which is what makes
 * carrying a record across one possible at all. A power cycle does lose it,
 * and should -- a crash from before the battery came out is not news. */
static __noinit struct fault_record record;

/* Reporting is main.c's job -- this file only has to survive the reset,
 * and a log module here would emit nothing.
 *
 * Replaces the weak default, which logs a banner and halts. Halting is the
 * problem: it leaves the board dead until the watchdog notices thirty seconds
 * later, and the banner never leaves the USB buffer. Record, then reboot. */
void k_sys_fatal_error_handler(unsigned int reason, const struct arch_esf *esf)
{
    /* Read before anything is overwritten: a fault that arrives soon after
     * boot, when the last boot also ended in one, is the same fault coming
     * round again rather than a new one. */
    uint32_t up      = (uint32_t) k_uptime_get();
    bool     looping = (record.magic == FAULT_MAGIC) && (up < FAULT_LOOP_MS);

    record.magic     = FAULT_MAGIC;
    record.reason    = reason;
    record.uptime_ms = up;
    record.pc        = 0;
    record.lr        = 0;
    record.psr       = 0;

#if defined(CONFIG_ARM) && !defined(CONFIG_ARMV8_M_MAINLINE)
    if (esf != NULL) {
        record.pc  = esf->basic.pc;
        record.lr  = esf->basic.lr;
        record.psr = esf->basic.xpsr;
    }
#else
    ARG_UNUSED(esf);
#endif

    record.thread[0]  = '\0';
    record.stack_used = 0;
    record.stack_size = 0;

    struct k_thread *cur = k_current_get();
    if (cur != NULL) {
        const char *name = k_thread_name_get(cur);
        if (name != NULL) {
            strncpy(record.thread, name, sizeof(record.thread) - 1);
            record.thread[sizeof(record.thread) - 1] = '\0';
        }
#ifdef CONFIG_THREAD_STACK_INFO
        record.stack_size = (uint32_t) cur->stack_info.size;
        /* Deliberately not k_thread_stack_space_get(): it walks the stack
         * looking for the fill pattern, and this is a fault handler on a
         * stack that may be the very thing that overflowed. The size alone,
         * next to the reason, is enough to tell a stack fault apart. */
#endif
    }

    /* Reboot rather than halt, which is what the default handler does.
     *
     * Halting is not a safe resting state on this board: nothing feeds the
     * watchdog once the CPU stops, so thirty seconds later it reboots
     * regardless. All halting buys is half a minute of a device that looks
     * dead and cannot be talked to. Rebooting immediately gets the shell
     * back, and gets this record read out.
     *
     * That does mean a fault which recurs every boot loops. It already did
     * -- the watchdog saw to that -- so the counter below is here to say so
     * out loud rather than to try to stop it. Anything that faults on every
     * boot needs a reflash, and the shell is reachable between cycles to ask
     * for one.
     */
    record.repeats = looping ? record.repeats + 1 : 1;

    /* A warm reset keeps RAM, which is the whole point of the record. */
    sys_reboot(SYS_REBOOT_WARM);

    CODE_UNREACHABLE;
}

bool fault_last(struct fault_record *out)
{
    if (record.magic != FAULT_MAGIC) {
        return false;
    }
    *out = record;
    return true;
}

void fault_clear(void)
{
    record.magic = 0;
}

const char *fault_reason_name(uint32_t reason)
{
    switch (reason) {
    case K_ERR_CPU_EXCEPTION:      return "cpu exception";
    case K_ERR_SPURIOUS_IRQ:       return "spurious irq";
    case K_ERR_STACK_CHK_FAIL:     return "stack overflow";
    case K_ERR_KERNEL_OOPS:        return "kernel oops";
    case K_ERR_KERNEL_PANIC:       return "kernel panic";
    default:                       return "unknown";
    }
}
