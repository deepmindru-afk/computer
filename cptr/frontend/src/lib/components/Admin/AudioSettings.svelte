<script lang="ts">
	import { toast } from 'svelte-sonner';
	import ToggleSwitch from '../common/ToggleSwitch.svelte';
	import Spinner from '../common/Spinner.svelte';
	import { onMount } from 'svelte';
	import { getAdminConfig, updateConfig } from '$lib/apis/admin';
	import { t } from '$lib/i18n';
	import { refreshAudioState } from '$lib/stores/audio';

	let loading = $state(true);
	let saving = $state(false);

	// Config state
	let voiceMemosEnabled = $state(false);
	let transcribeEnabled = $state(true);
	let quality = $state<'high' | 'medium' | 'low'>('high');
	let sttBaseUrl = $state('https://api.openai.com/v1');
	let sttApiKey = $state('');
	let sttModel = $state('whisper-1');
	let hasExistingKey = $state(false);

	onMount(async () => {
		try {
			const config = await getAdminConfig();
			voiceMemosEnabled = config['audio.voice_memos_enabled'] === true;
			transcribeEnabled = config['audio.transcribe_enabled'] !== false;
			const q = config['audio.recording_quality'];
			if (q === 'medium' || q === 'low') quality = q;
			else quality = 'high';
			sttBaseUrl = (config['audio.stt_base_url'] as string) || 'https://api.openai.com/v1';
			sttModel = (config['audio.stt_model'] as string) || 'whisper-1';
			hasExistingKey = !!config['audio.stt_api_key'];
		} catch {}
		loading = false;
	});

	async function save() {
		saving = true;
		try {
			const cfg: Record<string, unknown> = {
				'audio.voice_memos_enabled': voiceMemosEnabled,
				'audio.transcribe_enabled': transcribeEnabled,
				'audio.recording_quality': quality,
				'audio.stt_base_url': sttBaseUrl,
				'audio.stt_model': sttModel
			};
			if (sttApiKey) {
				cfg['audio.stt_api_key'] = sttApiKey;
			}
			await updateConfig(cfg);
			if (sttApiKey) hasExistingKey = true;
			toast.success($t('settings.saved'));
			refreshAudioState();
		} catch {
			toast.error('Failed to save audio settings');
		} finally {
			saving = false;
		}
	}
</script>

<div class="flex flex-col min-h-full">
	<h2 class="text-sm font-medium text-gray-900 dark:text-white mb-4">Audio</h2>

	{#if loading}
		<div class="flex justify-center py-8"><Spinner size={16} /></div>
	{:else}
		<!-- Voice Notes -->
		<h3 class="text-xs text-gray-400 dark:text-gray-600 mb-2">Voice Memos</h3>

		<div class="flex flex-col gap-2.5">
			<label class="flex items-center justify-between cursor-pointer">
				<span class="text-xs text-gray-600 dark:text-gray-400">Enable Voice Memos</span>
				<ToggleSwitch value={voiceMemosEnabled} onchange={(v) => { voiceMemosEnabled = v; }} />
			</label>
			<p class="text-[11px] text-gray-400 dark:text-gray-600 -mt-1">
				Record voice memos from the "+" menu.
			</p>

			<label class="flex items-center justify-between cursor-pointer">
				<span class="text-xs text-gray-600 dark:text-gray-400">Auto-transcribe</span>
				<ToggleSwitch value={transcribeEnabled} onchange={(v) => { transcribeEnabled = v; }} />
			</label>
			<p class="text-[11px] text-gray-400 dark:text-gray-600 -mt-1">
				{transcribeEnabled ? 'Recordings are transcribed to markdown via STT.' : 'Recordings are saved as audio only.'}
			</p>

			<div class="flex items-center justify-between">
				<span class="text-xs text-gray-600 dark:text-gray-400">Recording quality</span>
				<select
					bind:value={quality}
					class="bg-transparent text-xs text-gray-600 dark:text-gray-400 outline-none cursor-pointer"
				>
					<option value="high">High (128kbps)</option>
					<option value="medium">Medium (64kbps)</option>
					<option value="low">Low (32kbps)</option>
				</select>
			</div>
			<p class="text-[11px] text-gray-400 dark:text-gray-600 -mt-1">
				{quality === 'high' ? 'Best quality, larger files.' : quality === 'medium' ? 'Balanced quality and size.' : 'Smallest files, optimized for speech.'}
			</p>
		</div>

		<!-- Speech-to-Text -->
		<h3 class="text-xs text-gray-400 dark:text-gray-600 mb-2 mt-5">Speech-to-Text</h3>

		<div class="flex flex-col gap-2.5">
			<div>
				<label class="text-xs text-gray-600 dark:text-gray-400" for="stt-base-url">Base URL</label>
				<input
					id="stt-base-url"
					type="text"
					bind:value={sttBaseUrl}
					placeholder="https://api.openai.com/v1"
					class="w-full mt-1 h-7 px-2 rounded-lg text-xs bg-gray-100 dark:bg-white/6 text-gray-700 dark:text-gray-300 border border-gray-200 dark:border-white/8 outline-none focus:border-blue-400 dark:focus:border-blue-500 transition-colors"
				/>
			</div>
			<div>
				<label class="text-xs text-gray-600 dark:text-gray-400" for="stt-api-key">API Key</label>
				<input
					id="stt-api-key"
					type="password"
					bind:value={sttApiKey}
					placeholder={hasExistingKey ? '••••••••' : 'sk-...'}
					class="w-full mt-1 h-7 px-2 rounded-lg text-xs bg-gray-100 dark:bg-white/6 text-gray-700 dark:text-gray-300 border border-gray-200 dark:border-white/8 outline-none focus:border-blue-400 dark:focus:border-blue-500 transition-colors"
				/>
			</div>
			<div>
				<label class="text-xs text-gray-600 dark:text-gray-400" for="stt-model">Model</label>
				<input
					id="stt-model"
					type="text"
					bind:value={sttModel}
					placeholder="whisper-1"
					class="w-full mt-1 h-7 px-2 rounded-lg text-xs bg-gray-100 dark:bg-white/6 text-gray-700 dark:text-gray-300 border border-gray-200 dark:border-white/8 outline-none focus:border-blue-400 dark:focus:border-blue-500 transition-colors"
				/>
			</div>
			<p class="text-[11px] text-gray-400 dark:text-gray-600">
				Compatible with OpenAI's audio/transcriptions API.
			</p>
		</div>

		<!-- Save -->
		<div class="mt-auto pt-6 flex justify-end">
			<button
				class="text-[13px] text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-white transition-colors duration-100 disabled:opacity-50"
				onclick={() => save()}
				disabled={saving}
			>{$t('settings.save')}</button>
		</div>
	{/if}
</div>
