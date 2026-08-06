/* Link the fixture into the RTU0 CodeRAM window the loader accepts. */
ENTRY(_start)
SECTIONS {
  . = 0x32100000;
  .text   : { *(.text*) }
  .rodata : { *(.rodata*) }
  .data   : { *(.data*) }
  .bss    : { *(.bss*) }
}
