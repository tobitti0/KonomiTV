<template>
    <v-dialog max-width="650" transition="slide-y-transition" :model-value="show"
        :persistent="is_loading || is_saving" @update:model-value="$emit('update:show', $event)">
        <v-card class="box-recorded-file-dialog">
            <v-card-title class="d-flex align-center px-6 pt-6 pb-2 font-weight-bold">
                <Icon icon="fluent:cloud-link-20-filled" width="23px" height="23px" />
                <span class="ml-3">Box 録画ファイルの紐付け</span>
            </v-card-title>
            <v-card-text class="px-6 pt-3 pb-0">
                <v-alert class="mb-5" color="info" variant="tonal" density="compact">
                    入力した file ID の1件だけを確認します。Box 内のフォルダ探索は行いません。
                </v-alert>

                <v-skeleton-loader v-if="is_loading" type="list-item-two-line, text"></v-skeleton-loader>
                <template v-else>
                    <div v-if="current_link" class="box-recorded-file-dialog__current mb-5">
                        <div class="d-flex align-center mb-3">
                            <span class="font-weight-bold">現在の紐付け</span>
                            <v-chip class="ml-3" size="small"
                                :color="current_link.availability === 'Available' ? 'success' : 'error'">
                                {{availabilityLabel}}
                            </v-chip>
                        </div>
                        <dl>
                            <dt>Box file ID</dt>
                            <dd>{{current_link.box_file_id}}</dd>
                            <dt>ファイル名</dt>
                            <dd>{{current_link.name}}</dd>
                            <dt>ファイルサイズ</dt>
                            <dd>{{Utils.formatBytes(current_link.size)}}</dd>
                            <dt>登録方法</dt>
                            <dd>{{current_link.match_method}}</dd>
                            <dt>最終確認</dt>
                            <dd>{{dayjs(current_link.last_synced_at).format('YYYY/MM/DD HH:mm:ss')}}</dd>
                        </dl>
                    </div>
                    <div v-else class="mb-5 text-text-darken-1">この録画番組には Box file ID が登録されていません。</div>

                    <v-text-field v-model="box_file_id" label="Box file ID" variant="outlined"
                        inputmode="numeric" autocomplete="off" clearable
                        :disabled="is_saving" :rules="boxFileIDRules"
                        hint="Box の共有URL末尾にある数値を入力してください" persistent-hint>
                    </v-text-field>
                </template>
            </v-card-text>
            <v-card-actions class="px-6 pt-5 pb-6">
                <v-btn v-if="current_link" color="error" variant="text" :disabled="is_loading || is_saving"
                    @click="show_unlink_confirmation = true">
                    <Icon icon="fluent:link-dismiss-20-regular" width="18px" height="18px" />
                    <span class="ml-1">紐付け解除</span>
                </v-btn>
                <v-spacer></v-spacer>
                <v-btn color="text" variant="text" :disabled="is_saving" @click="$emit('update:show', false)">
                    閉じる
                </v-btn>
                <v-btn class="px-4" color="primary" variant="flat" :loading="is_saving"
                    :disabled="is_loading || isFileIDValid === false" @click="saveLink">
                    {{current_link ? '登録を変更' : '登録'}}
                </v-btn>
            </v-card-actions>
        </v-card>
    </v-dialog>

    <v-dialog max-width="590" v-model="show_unlink_confirmation">
        <v-card>
            <v-card-title class="px-6 pt-6 font-weight-bold">Box の紐付けを解除しますか？</v-card-title>
            <v-card-text class="px-6 pt-3 pb-0">
                Box 上の TS ファイルは削除されません。
                <div v-if="program.recorded_video.storage_type === 'Box'" class="mt-3 text-error-lighten-1 font-weight-bold">
                    この録画は Box にしか存在しません。解除すると再生できなくなり、次回の録画スキャンで番組情報が削除される可能性があります。
                </div>
            </v-card-text>
            <v-card-actions class="px-6 pt-4 pb-6">
                <v-spacer></v-spacer>
                <v-btn color="text" variant="text" @click="show_unlink_confirmation = false">キャンセル</v-btn>
                <v-btn color="error" variant="flat" :loading="is_saving" @click="unlink">紐付け解除</v-btn>
            </v-card-actions>
        </v-card>
    </v-dialog>
</template>
<script lang="ts" setup>

import { computed, ref, watch } from 'vue';

import type { IRecordedProgram } from '@/services/Videos';

import Message from '@/message';
import BoxRecordedFiles, { type IBoxRecordedFileLink } from '@/services/extensions/BoxRecordedFiles';
import Utils, { dayjs } from '@/utils';


const props = defineProps<{
    program: IRecordedProgram;
    show: boolean;
}>();

const emit = defineEmits<{
    (e: 'update:show', show: boolean): void;
    (e: 'linked', link: IBoxRecordedFileLink): void;
    (e: 'unlinked'): void;
}>();

const current_link = ref<IBoxRecordedFileLink | null>(null);
const box_file_id = ref('');
const is_loading = ref(false);
const is_saving = ref(false);
const show_unlink_confirmation = ref(false);

const isFileIDValid = computed(() => /^\d+$/.test(box_file_id.value.trim()));
const boxFileIDRules = [
    (value: string) => /^\d+$/.test(value?.trim() ?? '') || 'Box file ID は数字だけで入力してください。',
];
const availabilityLabel = computed(() => {
    if (current_link.value?.availability === 'Available') return '利用可能';
    if (current_link.value?.availability === 'Missing') return '見つかりません';
    return '取得エラー';
});

watch(() => props.show, async (show) => {
    if (show === false) return;
    is_loading.value = true;
    const link = await BoxRecordedFiles.fetchLink(props.program.id);
    if (link !== undefined) {
        current_link.value = link;
        box_file_id.value = link?.box_file_id ?? props.program.recorded_video.box_file_id ?? '';
    }
    is_loading.value = false;
});

const saveLink = async () => {
    if (isFileIDValid.value === false) return;
    is_saving.value = true;
    const link = await BoxRecordedFiles.link(props.program.id, box_file_id.value.trim());
    if (link !== null) {
        current_link.value = link;
        box_file_id.value = link.box_file_id;
        emit('linked', link);
        Message.success(`Box file ID ${link.box_file_id} を登録しました。`);
    }
    is_saving.value = false;
};

const unlink = async () => {
    is_saving.value = true;
    if (await BoxRecordedFiles.unlink(props.program.id)) {
        current_link.value = null;
        box_file_id.value = '';
        emit('unlinked');
        show_unlink_confirmation.value = false;
        Message.success('Box 録画の紐付けを解除しました。Box 上のファイルは変更していません。');
    }
    is_saving.value = false;
};

</script>
<style lang="scss" scoped>

.box-recorded-file-dialog {
    &__current {
        padding: 14px 16px;
        border-radius: 6px;
        background: rgb(var(--v-theme-background-lighten-2));

        dl {
            display: grid;
            grid-template-columns: 105px minmax(0, 1fr);
            gap: 7px 14px;
            margin: 0;
            font-size: 13px;
        }
        dt {
            color: rgb(var(--v-theme-text-darken-1));
        }
        dd {
            min-width: 0;
            margin: 0;
            overflow-wrap: anywhere;
        }
    }
}

</style>
