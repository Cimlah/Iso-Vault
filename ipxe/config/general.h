/* Iso-Vault build options, layered over iPXE's src/config/general.h */

/* Commands used by the boot menu */
#define REBOOT_CMD
#define POWEROFF_CMD
#define PARAM_CMD
#define CONSOLE_CMD

/* Debian and older Ubuntu ship gzip-compressed ARM64 kernels */
#define IMAGE_GZIP
#define IMAGE_ZLIB
#define IMAGE_ARCHIVE_CMD

/* Used by the generic recipe for ISOs without a known profile */
#define SANBOOT_PROTO_HTTP
