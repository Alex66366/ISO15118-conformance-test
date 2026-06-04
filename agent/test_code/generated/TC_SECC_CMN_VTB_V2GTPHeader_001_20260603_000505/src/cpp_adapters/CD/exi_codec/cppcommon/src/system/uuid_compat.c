/* uuid_generate wrapper - provides missing symbol using uuid_generate_random */
#include <stdint.h>
#include <uuid/uuid.h>

void uuid_generate(uint8_t out[16]) {
    uuid_generate_random(out);
}
