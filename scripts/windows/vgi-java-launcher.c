/*
 * vgi-java-launcher.c — single-token Windows subprocess worker for the Java
 * example worker.
 *
 * Why a native wrapper is needed (all three bite on Windows):
 *   1. VGI's subprocess LOCATION is whitespace-tokenized, so the space in
 *      "C:\Program Files\...\java.exe" splits the command.
 *   2. The gradle installDist app jar has no Main-Class, so `java -jar` fails;
 *      it needs `-cp <lib>\* farm.query.vgi.example.Main`.
 *   3. `_execl` orphans the JVM (Windows has no real exec) — VGI loses its child
 *      and sees EOF. `_spawnl(_P_WAIT, ...)` keeps this wrapper alive holding the
 *      pipes while the JVM inherits them.
 *
 * The wrapper is a single executable token VGI can spawn; it reads config from
 * the environment so it never needs recompiling per machine:
 *   VGI_JAVA_EXE   full path to java.exe                              (required)
 *   VGI_JAVA_CP    classpath, e.g.  C:\...\vgi-example-worker\lib\*   (required)
 *   VGI_JAVA_MAIN  main class    (default: farm.query.vgi.example.Main)
 *
 * Build (either compiler):
 *   gcc vgi-java-launcher.c -o vgi-java-worker.exe
 *   cl  vgi-java-launcher.c /Fe:vgi-java-worker.exe
 *
 * Then point the java adapter's subprocess transport at vgi-java-worker.exe
 * (it defaults to ~/vgi-java/vgi-java-worker) and export the two env vars.
 */
#include <process.h>
#include <stdio.h>
#include <stdlib.h>

int main(void) {
    const char *java = getenv("VGI_JAVA_EXE");
    const char *cp = getenv("VGI_JAVA_CP");
    const char *main_class = getenv("VGI_JAVA_MAIN");
    if (main_class == NULL) {
        main_class = "farm.query.vgi.example.Main";
    }
    if (java == NULL || cp == NULL) {
        fprintf(stderr, "vgi-java-launcher: set VGI_JAVA_EXE and VGI_JAVA_CP\n");
        return 2;
    }
    /* The JVM inherits this process's stdio (VGI's pipes); we block until it exits. */
    intptr_t rc = _spawnl(_P_WAIT, java, "java",
                          "--add-opens=java.base/java.nio=ALL-UNNAMED",
                          "--enable-native-access=ALL-UNNAMED",
                          "-cp", cp, main_class, (char *)0);
    if (rc == -1) {
        perror("vgi-java-launcher: _spawnl");
        return 127;
    }
    return (int)rc;
}
