/* Copyright (C) 2026 John Törnblom

This program is free software; you can redistribute it and/or modify it
under the terms of the GNU General Public License as published by the
Free Software Foundation; either version 3, or (at your option) any
later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program; see the file COPYING. If not, see
<http://www.gnu.org/licenses/>.  */

#include <errno.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include <sys/mount.h>
#include <sys/stat.h>
#include <sys/uio.h>

#include <ps5/kernel.h>


#define IOVEC_SIZE(x) (sizeof(x) / sizeof(struct iovec))
#define IOVEC_ENTRY(x) {x ? x : 0, x ? strlen(x)+1 : 0}

#define INCASSET(name, file)			\
  __asm__(".section .rodata\n"			\
	  ".global " #name "\n"			\
	  ".global " #name "_end\n"		\
	  ".global " #name "_size\n"		\
	  ".align 16\n"				\
	  #name ":\n"				\
	  ".incbin \"" file "\"\n"		\
	  #name "_end:\n"			\
	  #name "_size:\n"			\
	  ".quad " #name "_end - " #name "\n"	\
	  ".previous\n");			\
  extern const uint8_t name[];			\
  extern const size_t name##_size;


int sceAppInstUtilInitialize(void);
int sceAppInstUtilAppInstallAll(void*);
int sceAppInstUtilAppUnInstall(const char*);


INCASSET(param_json, "sce_sys/param.json");
INCASSET(icon0_png, "sce_sys/icon0.png");
INCASSET(pic1_png, "sce_sys/pic1.png");
INCASSET(pic0_dds, "sce_sys/pic0.dds");
INCASSET(pic0_png, "sce_sys/pic0.png");
INCASSET(snd0_at9, "sce_sys/snd0.at9");


static int
remount_system_ex(void) {
  struct iovec iov[] = {
    IOVEC_ENTRY("from"),      IOVEC_ENTRY("/dev/ssd0.system_ex"),
    IOVEC_ENTRY("fspath"),    IOVEC_ENTRY("/system_ex"),
    IOVEC_ENTRY("fstype"),    IOVEC_ENTRY("exfatfs"),
    IOVEC_ENTRY("large"),     IOVEC_ENTRY("yes"),
    IOVEC_ENTRY("timezone"),  IOVEC_ENTRY("static"),
    IOVEC_ENTRY("async"),     IOVEC_ENTRY(NULL),
    IOVEC_ENTRY("ignoreacl"), IOVEC_ENTRY(NULL),
  };

  return nmount(iov, IOVEC_SIZE(iov), MNT_UPDATE);
}

typedef struct notify_request {
  char useless1[45];
  char message[3075];
} notify_request_t;


int sceKernelSendNotificationRequest(int, notify_request_t*, size_t, int);


int notify(char* msg) {
  notify_request_t req;

  bzero(&req, sizeof req);
  strncpy(req.message, msg, sizeof req.message);

  return sceKernelSendNotificationRequest(0, &req, sizeof req, 0);
}


static int
install_file(const char* path, const uint8_t* data, size_t size) {
  FILE* f;

  if(!(f=fopen(path, "w"))) {
    return -1;
  }

  if(data && size) {
    if(fwrite(data, size, 1, f) != 1) {
      fclose(f);
      return -1;
    }
  }

  fclose(f);
  return 0;
}


static int
install_app(const char* title_id, const char* dir) {
  int (*sceAppInstUtilAppInstallTitleDir)(const char*, const char*, void*) = 0;
  const char* nid = "Wudg3Xe3heE";
  uint32_t handle;

  if(!kernel_dynlib_handle(-1, "libSceAppInstUtil.sprx", &handle)) {
    sceAppInstUtilAppInstallTitleDir = (void*)kernel_dynlib_resolve(-1, handle, nid);
  }

  if(sceAppInstUtilAppInstallTitleDir) {
    return sceAppInstUtilAppInstallTitleDir(title_id, dir, 0);
  }

  return sceAppInstUtilAppInstallAll(0);
}


int
main(int argc, char *argv[]) {
  int err;

  puts("@@INSTALL_MSG@@");
  notify("@@INSTALL_MSG@@");

  if((err=sceAppInstUtilInitialize())) {
    printf("sceAppInstUtilInitialize: error 0x%08X\n", err);
    return -1;
  }

  sceAppInstUtilAppUnInstall(TITLE_ID);

  remount_system_ex();

  if(mkdir("/system_ex/app/"TITLE_ID, 0755) && errno != EEXIST) {
    perror("mkdir");
    return -1;
  }
  if(mkdir("/system_ex/app/"TITLE_ID"/sce_sys", 0755) && errno != EEXIST) {
    perror("mkdir");
    return -1;
  }

  if(install_file("/system_ex/app/"TITLE_ID"/eboot.bin", 0, 0)) {
    perror("install_file");
    return -1;
  }
  if(install_file("/system_ex/app/"TITLE_ID"/sce_sys/param.json",
		  param_json, param_json_size)) {
    perror("install_file");
    return -1;
  }

  if(mkdir("/user/app/"TITLE_ID, 0755) && errno != EEXIST) {
    perror("mkdir");
    return -1;
  }
  if(mkdir("/user/app/"TITLE_ID"/sce_sys", 0755) && errno != EEXIST) {
    perror("mkdir");
    return -1;
  }
  if(install_file("/user/app/"TITLE_ID"/sce_sys/icon0.png",
		  icon0_png, icon0_png_size)) {
    perror("install_file");
    return -1;
  }
  if(install_file("/user/app/"TITLE_ID"/sce_sys/pic1.png",
		  pic1_png, pic1_png_size)) {
    perror("install_file");
    return -1;
  }
  if(install_file("/user/app/"TITLE_ID"/sce_sys/pic0.dds",
		  pic0_dds, pic0_dds_size)) {
    perror("install_file");
    return -1;
  }
  if(install_file("/user/app/"TITLE_ID"/sce_sys/pic0.png",
		  pic0_png, pic0_png_size)) {
    perror("install_file");
    return -1;
  }
  if(install_file("/user/app/"TITLE_ID"/sce_sys/snd0.at9",
		  snd0_at9, snd0_at9_size)) {
    perror("install_file");
    return -1;
  }
  if(install_file("/user/app/"TITLE_ID"/sce_sys/param.json",
		  param_json, param_json_size)) {
    perror("install_file");
    return -1;
  }

  if((err=install_app(TITLE_ID, "/user/app/"))) {
    printf("install_app: error 0x%08X\n", err);
    return -1;
  }

  puts("@@SUCCESS_MSG@@");
  notify("@@SUCCESS_MSG@@");

  return 0;
}
