<template>
    <!-- DPlayer の設定パネルへ Teleport し、本体コードへの変更を最小限に保つ -->
    <Teleport v-if="setting_panel_target !== null" :to="setting_panel_target">
        <div class="dplayer-setting-item dplayer-setting-comment-intensity-graph"
            @click.stop="commentIntensityGraphState.is_enabled.value = !commentIntensityGraphState.is_enabled.value">
            <span class="dplayer-label">コメント勢いグラフ</span>
            <div class="dplayer-toggle">
                <input id="dplayer-toggle-comment-intensity-graph"
                    class="dplayer-comment-intensity-graph-setting-input"
                    type="checkbox"
                    :checked="commentIntensityGraphState.is_enabled.value"
                    tabindex="-1"
                    @click.prevent>
                <label for="dplayer-toggle-comment-intensity-graph"
                    style="--theme-color:#E64F97"
                    @click.prevent></label>
            </div>
        </div>
    </Teleport>
</template>
<script setup lang="ts">

import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue';

import useCommentIntensityGraphState from '@/components/Watch/CommentIntensityGraphState';

const commentIntensityGraphState = useCommentIntensityGraphState();
const setting_panel_target = ref<HTMLElement | null>(null);
let player_container_observer: MutationObserver | null = null;

// DPlayer は Vue コンポーネントのマウント後に構築・再構築されるため、DOM の変化から Teleport 先を追従する
const updateSettingPanelTarget = (): void => {
    const next_target = document.querySelector<HTMLElement>(
        '.watch-player__dplayer .dplayer-setting-origin-panel',
    );
    if (setting_panel_target.value !== next_target) {
        setting_panel_target.value = next_target;
    }
};

onMounted(async () => {
    await nextTick();
    const player_container = document.querySelector<HTMLElement>('.watch-player__dplayer');
    updateSettingPanelTarget();
    if (player_container !== null) {
        player_container_observer = new MutationObserver(() => {
            updateSettingPanelTarget();
        });
        player_container_observer.observe(player_container, {
            childList: true,
            subtree: true,
        });
    }
});

onBeforeUnmount(() => {
    if (player_container_observer !== null) {
        player_container_observer.disconnect();
        player_container_observer = null;
    }
});

</script>
