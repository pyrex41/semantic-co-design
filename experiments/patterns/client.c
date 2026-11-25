#include "patterns.h"
#include <inttypes.h>
#include <stdio.h>

int main(void) {
    uint32_t amount[] = {125}, charges[] = {4, 10, 20}, event[] = {7, 42, 9, 42};
    printf("queue=%" PRId64 " total=%" PRId64 " transfer=%" PRId64 "\n",
           pat_review_queue(amount, 1), pat_billable_total(charges, 3), pat_transfer_id(event, 4));
    return 0;
}
