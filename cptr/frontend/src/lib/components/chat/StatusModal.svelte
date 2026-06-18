<script lang="ts">
	import Modal from '../Modal.svelte';
	import type { ContextUsage } from '$lib/apis/chat';
	import { t } from '$lib/i18n';

	interface Props {
		chatId: string | null;
		contextUsage: ContextUsage | null;
		onclose: () => void;
	}

	let { chatId, contextUsage, onclose }: Props = $props();

	let copied = $state(false);
	const contextPercent = $derived(Math.max(0, Math.round(contextUsage?.percent ?? 0)));
	const progressWidth = $derived(`${Math.min(contextPercent, 100)}%`);
	const tokenCount = $derived(contextUsage?.tokens ?? contextUsage?.estimated_tokens ?? 0);

	function copyChatId() {
		if (!chatId) return;
		navigator.clipboard.writeText(chatId);
		copied = true;
		setTimeout(() => (copied = false), 1600);
	}
</script>

<Modal {onclose} class="w-full max-w-[300px] mx-4">
	<div class="px-4 py-3.5">
		<div class="flex items-baseline justify-between gap-3 mb-4">
			<h2 class="text-sm font-medium text-gray-900 dark:text-white">
				{$t('chat.commandStatus')}
			</h2>
			<span class="text-[11px] font-mono text-gray-400 dark:text-gray-600">
				{contextUsage ? `${contextPercent}%` : $t('chat.statusUnknown')}
			</span>
		</div>

		<div class="mb-4">
			<div class="h-1 overflow-hidden rounded-full bg-gray-100 dark:bg-white/6">
				<div
					class="h-full rounded-full bg-gray-900 dark:bg-white transition-[width] duration-200"
					style={`width: ${progressWidth};`}
				></div>
			</div>
			<div class="mt-1.5 flex items-baseline justify-between gap-3">
				<span class="text-[10px] text-gray-400 dark:text-gray-600">
					{$t('chat.statusEstimated')}
				</span>
				<span class="font-mono text-[10px] text-gray-400 dark:text-gray-600">
					{#if contextUsage}
						{tokenCount.toLocaleString()} / {contextUsage.threshold.toLocaleString()}
					{:else}
						{$t('chat.statusUnknown')}
					{/if}
				</span>
			</div>
		</div>

		<div>
			<div class="text-[10px] text-gray-400 dark:text-gray-600 mb-1">
				{$t('chat.statusChatId')}
			</div>
			{#if chatId}
				<button
					type="button"
					class="block w-full text-left font-mono text-[11px] leading-relaxed text-gray-700 dark:text-gray-300 break-all hover:text-gray-900 dark:hover:text-white transition-colors"
					onclick={copyChatId}
				>
					{copied ? $t('about.copied') : chatId}
				</button>
			{:else}
				<div class="font-mono text-[11px] leading-relaxed text-gray-700 dark:text-gray-300">
					{$t('chat.statusNoChat')}
				</div>
			{/if}
		</div>
	</div>
</Modal>
