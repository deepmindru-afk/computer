<script lang="ts">
	import Modal from '../Modal.svelte';
	import { t } from '$lib/i18n';
	import type { AgentProfile } from '$lib/apis/admin';

	interface Props {
		profile: AgentProfile;
		isNew?: boolean;
		onclose: () => void;
		onsave: (profile: AgentProfile) => void;
		ondelete: () => void;
	}

	let { profile, isNew = false, onclose, onsave, ondelete }: Props = $props();
	const fieldPrefix = `agent-profile-${Math.random().toString(36).slice(2)}`;

	// svelte-ignore state_referenced_locally
	let draft = $state<AgentProfile>({
		...profile,
		models: [...(profile.models?.length ? profile.models : ['default'])]
	});

	function normalizeDraft(): AgentProfile {
		const fallbackModel = draft.agent === 'codex' ? 'gpt-5.4' : 'claude-sonnet-4-6';
		const currentModels = draft.models?.map((model) => model.trim()).filter(Boolean) || [];
		const models =
			currentModels.length === 1 && currentModels[0] === 'default'
				? [fallbackModel]
				: currentModels.length
					? currentModels
					: [fallbackModel];
		const defaultModel =
			draft.default_model !== 'default' && models.includes(draft.default_model)
				? draft.default_model
				: models[0];
		return {
			...draft,
			id: draft.id.trim(),
			name: draft.name.trim() || draft.id.trim(),
			command: draft.command.trim() || (draft.agent === 'codex' ? 'codex' : 'claude'),
			home: draft.home?.trim() || null,
			models,
			default_model: defaultModel,
			launch_args: draft.launch_args?.trim() || ''
		};
	}
</script>

<Modal {onclose} class="w-full max-w-md mx-4">
	<!-- svelte-ignore a11y_no_static_element_interactions -->
	<div
		class="p-4"
		onkeydown={(e) => {
			if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) onsave(normalizeDraft());
		}}
	>
		<div class="flex items-center justify-between gap-3 mb-3">
			<h2 class="text-sm font-medium text-gray-900 dark:text-white">
				{isNew ? $t('admin.agentsAddProfile') : draft.name}
			</h2>
			<button
				type="button"
				class="text-[13px] text-gray-400 dark:text-gray-600 hover:text-gray-700 dark:hover:text-gray-300 transition-colors"
				onclick={onclose}
			>
				Close
			</button>
		</div>

		<div class="flex gap-3">
			<div class="flex-1">
				<label for={`${fieldPrefix}-name`} class="text-[10px] text-gray-400 dark:text-gray-600">
					{$t('admin.agentsProfileName')}
				</label>
				<input
					id={`${fieldPrefix}-name`}
					type="text"
					bind:value={draft.name}
					autocomplete="off"
					spellcheck="false"
					class="block w-full bg-transparent text-[13px] text-gray-700 dark:text-gray-300 placeholder:text-gray-300 dark:placeholder:text-gray-700 outline-none py-0.5"
				/>
			</div>
			<div class="w-32 shrink-0">
				<label for={`${fieldPrefix}-type`} class="text-[10px] text-gray-400 dark:text-gray-600">
					{$t('admin.agentsType')}
				</label>
				<select
					id={`${fieldPrefix}-type`}
					value={draft.agent}
					onchange={(e) => {
						const agent = e.currentTarget.value as 'codex' | 'claude_code';
						const defaultModel = agent === 'codex' ? 'gpt-5.4' : 'claude-sonnet-4-6';
						draft = {
							...draft,
							agent,
							command: agent === 'codex' ? 'codex' : 'claude',
							models: [defaultModel],
							default_model: defaultModel,
							approval_mode: draft.approval_mode || 'auto',
							sandbox_mode: draft.sandbox_mode || 'workspace-write',
							permission_mode: draft.permission_mode || 'default',
							launch_args: draft.launch_args || ''
						};
					}}
					class="block w-full bg-transparent text-[13px] text-gray-700 dark:text-gray-300 outline-none py-0.5 cursor-pointer"
				>
					<option value="codex">Codex</option>
					<option value="claude_code">Claude Code</option>
				</select>
			</div>
		</div>

		<label for={`${fieldPrefix}-id`} class="text-[10px] text-gray-400 dark:text-gray-600 mt-2">
			{$t('admin.agentsProfileId')}
		</label>
		<input
			id={`${fieldPrefix}-id`}
			type="text"
			bind:value={draft.id}
			autocomplete="off"
			spellcheck="false"
			class="block w-full bg-transparent text-[13px] text-gray-700 dark:text-gray-300 placeholder:text-gray-300 dark:placeholder:text-gray-700 outline-none py-0.5 font-mono"
		/>

		<label for={`${fieldPrefix}-command`} class="text-[10px] text-gray-400 dark:text-gray-600 mt-2">
			{$t('admin.agentsCommand')}
		</label>
		<input
			id={`${fieldPrefix}-command`}
			type="text"
			bind:value={draft.command}
			placeholder={draft.agent === 'codex' ? 'codex' : 'claude'}
			autocomplete="off"
			spellcheck="false"
			class="block w-full bg-transparent text-[13px] text-gray-700 dark:text-gray-300 placeholder:text-gray-300 dark:placeholder:text-gray-700 outline-none py-0.5 font-mono"
		/>

		<label for={`${fieldPrefix}-home`} class="text-[10px] text-gray-400 dark:text-gray-600 mt-2">
			{$t('admin.agentsHome')}
		</label>
		<input
			id={`${fieldPrefix}-home`}
			type="text"
			value={draft.home || ''}
			placeholder="Optional"
			autocomplete="off"
			spellcheck="false"
			oninput={(e) => (draft.home = e.currentTarget.value || null)}
			class="block w-full bg-transparent text-[13px] text-gray-700 dark:text-gray-300 placeholder:text-gray-300 dark:placeholder:text-gray-700 outline-none py-0.5 font-mono"
		/>

		{#if draft.agent === 'claude_code'}
			<label
				for={`${fieldPrefix}-launch-args`}
				class="text-[10px] text-gray-400 dark:text-gray-600 mt-2"
			>
				{$t('admin.agentsLaunchArgs')}
			</label>
			<input
				id={`${fieldPrefix}-launch-args`}
				type="text"
				bind:value={draft.launch_args}
				autocomplete="off"
				spellcheck="false"
				class="block w-full bg-transparent text-[13px] text-gray-700 dark:text-gray-300 placeholder:text-gray-300 dark:placeholder:text-gray-700 outline-none py-0.5 font-mono"
			/>
		{/if}

		<div class="flex items-center justify-between mt-3">
			<div class="flex items-center gap-3">
				{#if !isNew}
					<button
						type="button"
						class="text-[13px] text-gray-400 hover:text-red-500 dark:hover:text-red-400 transition-colors duration-100"
						onclick={ondelete}
						title={$t('admin.agentsDeleteProfile')}
					>
						{$t('admin.agentsDeleteProfile')}
					</button>
				{/if}
			</div>
			<button
				type="button"
				onclick={() => onsave(normalizeDraft())}
				class="text-[13px] text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-white transition-colors duration-100"
			>
				{$t('settings.save')}
			</button>
		</div>
	</div>
</Modal>
