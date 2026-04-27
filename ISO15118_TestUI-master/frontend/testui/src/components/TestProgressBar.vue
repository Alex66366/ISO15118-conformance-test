<template>
  <div class="flex flex-col gap-1">
    <div class="flex items-center justify-between text-xs">
      <span class="font-medium" :class="progressColor">{{ pass }} / {{ total }}</span>
      <span class="text-gray-500">{{ percentage }}%</span>
    </div>
    <div class="h-1.5 bg-dark-elevated rounded-full overflow-hidden">
      <div
        class="h-full rounded-full transition-all duration-500 ease-out"
        :class="progressBarColor"
        :style="{ width: `${percentage}%` }"
      ></div>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  pass: { type: Number, default: 0 },
  total: { type: Number, default: 0 },
  type: { type: String, default: 'default' }
})

const percentage = computed(() => {
  if (props.total === 0) return 0
  return Math.round((props.pass / props.total) * 100)
})

const progressColor = computed(() => {
  if (percentage.value >= 80) return 'text-accent'
  if (percentage.value >= 50) return 'text-yellow-400'
  return 'text-red-400'
})

const progressBarColor = computed(() => {
  if (percentage.value >= 80) return 'bg-accent'
  if (percentage.value >= 50) return 'bg-yellow-400'
  return 'bg-red-400'
})
</script>
