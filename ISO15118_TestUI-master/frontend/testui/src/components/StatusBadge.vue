<template>
  <el-tag :type="tagType" size="small" effect="dark" round>
    {{ statusText }}
  </el-tag>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  status: { type: String, default: 'n' }
})

const statusMap = {
  'p': { text: 'Pass', type: 'success' },
  'f': { text: 'Fail', type: 'danger' },
  'i': { text: 'Inconclusive', type: 'warning' },
  'e': { text: 'Error', type: 'danger' },
  'n': { text: 'None', type: 'info' }
}

const statusText = computed(() => statusMap[props.status]?.text || 'Unknown')
const tagType = computed(() => statusMap[props.status]?.type || 'info')
</script>
