#pragma once
struct shell;
/* Reports card state to the shell. 0 when the card round-trips a file. */
int sd_probe(const struct shell *sh);
