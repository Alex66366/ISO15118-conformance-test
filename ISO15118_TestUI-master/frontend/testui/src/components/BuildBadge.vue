<template>
  <el-tag :type="tagType" size="small" effect="dark" round>
    {{ statusText }}
  </el-tag>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  status: { type: String, default: 'NA' }
})

const statusMap = {
  'OK': { text: 'OK', type: 'success' },
  'NA': { text: 'N/A', type: 'info' }
}

const statusText = computed(() => statusMap[props.status]?.text || props.status)
const tagType = computed(() => statusMap[props.status]?.type || 'info')
</script>
