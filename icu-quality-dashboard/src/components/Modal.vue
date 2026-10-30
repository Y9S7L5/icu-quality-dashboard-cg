<template>
  <div class="mask" @click.self="$emit('close')">
    <div class="modal">
      <div class="modal-head">
        <span>{{ title }}</span>
        <span class="modal-head-right"><slot name="head-right" /></span>
        <span class="close" @click="$emit('close')">✕</span>
      </div>
      <div class="modal-body"><slot /></div>
    </div>
  </div>
</template>
<script setup>
defineProps({ title: String });
defineEmits(['close']);
</script>
<style scoped>
.mask { position:fixed; inset:0; background:rgba(0,0,0,0.4); display:flex;
  align-items:center; justify-content:center; z-index:100; }
.modal { background:var(--bg-surface); border:1px solid var(--border); border-radius:var(--radius-card);
  width:min(1080px,92vw); max-height:84vh; display:flex; flex-direction:column; box-shadow: var(--shadow-card); }
.modal-head { display:flex; align-items:center; padding:16px 20px;
  border-bottom:1px solid var(--border); color:var(--text-title); font-size:var(--fs-h1); font-weight:600; }
.modal-head-right { margin-left:auto; margin-right:12px; font-weight:normal; }
.close { cursor:pointer; color:var(--text-sub); }
.close:hover { color:var(--text-title); }
/* flex 列布局：让槽位内容（如 DetailModal）能感知弹窗可用高度，
   配合内部 flex:1 把表格区高度约束在可视区内（横向滚动条固定在底部） */
.modal-body { padding:18px 20px; overflow-y:auto; display:flex; flex-direction:column; flex:1; min-height:0; }
</style>
