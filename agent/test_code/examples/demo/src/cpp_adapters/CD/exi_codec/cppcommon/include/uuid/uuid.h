#ifndef _UUID_UUID_H
#define _UUID_UUID_H

#ifdef __cplusplus
extern "C" {
#endif

#include <stdint.h>
#include <sys/time.h>

typedef uint8_t uuid_t[16];

void uuid_generate(uuid_t out);
void uuid_clear(uuid_t uu);
int uuid_compare(const uuid_t uu1, const uuid_t uu2);
void uuid_copy(uuid_t dst, const uuid_t src);
int uuid_is_null(const uuid_t uu);
void uuid_unparse(const uuid_t uu, char *out);
void uuid_unparse_lower(const uuid_t uu, char *out);
void uuid_unparse_upper(const uuid_t uu, char *out);
int uuid_parse(const char *in, uuid_t uu);
time_t uuid_time(const uuid_t uu, struct timeval *ret_tv);
int uuid_type(const uuid_t uu);
int uuid_variant(const uuid_t uu);
void uuid_generate_time(uuid_t out);
void uuid_generate_random(uuid_t out);

#ifdef __cplusplus
}
#endif

#endif
