/*
 * web_fs.c - where the game's files are in a browser (Phase 2, ADR-0005).
 *
 * The page (web/aoe2.js) copies the owner's install tree into the origin private
 * file system (OPFS) under aoe2/ before it starts the module; here WASMFS's OPFS
 * backend is mounted at /opfs, so the game reads and writes it in place (saved
 * games, recordings, the player profile persist across visits). The install root
 * is then /opfs/aoe2, mapped to C: by main.c as on Linux.
 */
#ifdef __EMSCRIPTEN__
#include <stdio.h>
#include <sys/stat.h>

#include <emscripten/wasmfs.h>

const char *web_game_root(void) {
    static int mounted;
    if (!mounted) {
        backend_t opfs = wasmfs_create_opfs_backend();
        if (!opfs || wasmfs_create_directory("/opfs", 0777, opfs) != 0)
            fprintf(stderr, "[web] cannot mount the origin private file system\n");
        mounted = 1;
    }
    return "/opfs/aoe2";
}
#endif
