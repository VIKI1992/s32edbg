# Symbol-layer test fixture

`fixture.elf` is committed so the offline suite needs no toolchain. It was
built from `fixture.c` + `fixture.ld` with the project's own compiler (the
same GCC that builds the production images, so the DWARF flavor under test is
the DWARF flavor in the field):

```bat
set TC=C:\br1_rear_ecu\Tools\compiler\gcc_v10.2\gcc-10.2-arm32-eabi\bin
%TC%\arm-none-eabi-gcc.exe -mcpu=cortex-r52 -mthumb -g -O0 ^
  -nostdlib -nostartfiles -ffreestanding -Wl,--build-id=none ^
  -T fixture.ld -o fixture.elf fixture.c
```

Rebuild only when the tests need new shapes, and update any line-number
assumptions in `tests/test_symbols.py` (they locate lines by searching
`fixture.c`'s text, so plain rebuilds are usually safe).
