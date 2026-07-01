import { Button } from '@/components/ui/button';
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from '@/components/ui/collapsible';
import {
  FormControl,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from '@/components/ui/form';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import { useFetchBuiltinCompilationTemplates } from '@/hooks/use-compilation-template-request';
import { useFetchDefaultModelDictionary } from '@/hooks/use-llm-request';
import {
  BuiltinCompilationTemplate,
  CompilationTemplateKind,
} from '@/interfaces/database/compilation-template';
import { LLMSelect } from '@/pages/dataset/dataset-setting/configuration/common-item';
import { ChevronDown, ChevronRight, Trash2 } from 'lucide-react';
import { useCallback, useMemo, useState } from 'react';
import { useFormContext, useWatch } from 'react-hook-form';
import { useTranslation } from 'react-i18next';
import {
  buildFieldTemplateMaps,
  templateConfigToFormValues,
  TEXT_FIELD_MAX,
} from '../interface';
import { ArtifactExtras } from './artifact-extras';
import { BuiltinTemplatePopover } from './builtin-template-popover';
import { EntityRelationSection } from './entity-relation-section';
import { GlobalRulesBlock } from './global-rules-block';
import { TreeExtras } from './tree-extras';

interface ChildFormSectionProps {
  /** Sibling index in the group's children list. Currently unused
   * inside the section body, but kept on the prop surface so callers
   * can identify which child raised a callback without re-deriving it
   * from ``pathPrefix``. */
  index: number;
  pathPrefix: string;
  onRemove?: () => void;
  /**
   * Kinds the user is currently allowed to switch to. Computed by the
   * parent based on the group's existing children so the mutual
   * exclusion between artifacts and other kinds is enforced live.
   */
  allowedKinds: readonly CompilationTemplateKind[];
}

/**
 * One foldable child inside the template-group form. Header shows the
 * kind badge + name + remove; expanding the card reveals the per-kind
 * body identical to the original single-template editor — but rendered
 * against ``templates.${index}`` paths via the ``pathPrefix`` prop on
 * each sub-component.
 */
export function ChildFormSection({
  pathPrefix,
  onRemove,
}: ChildFormSectionProps) {
  const { t } = useTranslation();
  const form = useFormContext<any>();
  const [open, setOpen] = useState(true);
  const { data: builtins } = useFetchBuiltinCompilationTemplates();
  const defaultModelDictionary = useFetchDefaultModelDictionary();
  const defaultChatLlmId = defaultModelDictionary.llm_id;

  // Watch the kind so the conditional sections re-render when the user
  // switches kinds mid-edit. Using ``useWatch`` (not ``form.watch``)
  // keeps the parent form re-render scope tight.
  const watchedKind = useWatch({
    control: form.control,
    name: `${pathPrefix}.kind`,
  }) as CompilationTemplateKind | undefined;

  // Watched so the collapsible header can show the template's name
  // next to the kind chip. Falls back to a placeholder when empty.
  const watchedName = useWatch({
    control: form.control,
    name: `${pathPrefix}.name`,
  }) as string | undefined;

  // Page-structure example default-prefill for artifacts kind. Fires
  // when the textarea is currently empty so explicit user edits and
  // saved overrides are never clobbered.
  const watchedExample = useWatch({
    control: form.control,
    name: `${pathPrefix}.example`,
  }) as string | undefined;

  if (
    watchedKind === 'artifacts' &&
    (!watchedExample || !watchedExample.trim())
  ) {
    const artifactsBuiltin = builtins.find((b) => b.kind === 'artifacts');
    const def = (artifactsBuiltin?.config as { example?: string } | undefined)
      ?.example;
    if (def && def.trim()) {
      // Schedule a microtask so the form update doesn't fire during the
      // render that observed the empty value.
      queueMicrotask(() => {
        form.setValue(`${pathPrefix}.example`, def, { shouldDirty: false });
      });
    }
  }

  const fieldTemplates = useMemo(
    () => buildFieldTemplateMaps(builtins.map((b) => b.config)),
    [builtins],
  );

  const handleApplyBuiltin = useCallback(
    (builtin: BuiltinCompilationTemplate) => {
      const next = templateConfigToFormValues(
        watchedName?.trim() || builtin.display_name,
        '',
        builtin.config,
      );
      next.llm_id =
        next.llm_id ||
        form.getValues(`${pathPrefix}.llm_id`) ||
        defaultChatLlmId ||
        '';

      // ``form.setValue`` on a parent path updates the form data but
      // does NOT re-seed the internal state of ``useFieldArray``
      // instances that point into the subtree — RHF tracks each field
      // array's row keys in a per-hook store, and only ``reset`` (or
      // the array's own ``replace``) refreshes it. Without this, the
      // child's Entity / Relation / Claim / Concept repeaters would
      // continue to show their previous row count even after the
      // ``fields`` array under the new config has, say, 8 entries.
      //
      // Strategy: snapshot the full form, splice the targeted child
      // path, then ``reset`` with ``keepDirty`` so the group's submit
      // path still treats the form as modified.
      const all = form.getValues() as any;
      const parts = pathPrefix.split('.');
      let target = all;
      for (let i = 0; i < parts.length - 1; i++) {
        target = target?.[parts[i]];
        if (!target) return;
      }
      target[parts[parts.length - 1]] = next;
      form.reset(all, {
        keepDirty: true,
        keepTouched: true,
        keepSubmitCount: true,
      });
    },
    [defaultChatLlmId, form, pathPrefix, watchedName],
  );

  const kindLabel = watchedKind
    ? t(`knowledgeCompilation.kind.${watchedKind}`)
    : '';

  return (
    <Collapsible
      open={open}
      onOpenChange={setOpen}
      className="rounded-md border border-border-button bg-bg-base"
    >
      <div className="flex items-center justify-between gap-2 p-3">
        <CollapsibleTrigger asChild>
          <button
            type="button"
            className="flex items-center gap-2 text-left flex-1 min-w-0"
          >
            {open ? (
              <ChevronDown className="size-4 shrink-0" />
            ) : (
              <ChevronRight className="size-4 shrink-0" />
            )}
            <span className="text-xs uppercase tracking-wide rounded bg-bg-card px-2 py-0.5 shrink-0">
              {kindLabel || t('knowledgeCompilation.kind.empty')}
            </span>
            <span className="truncate font-medium" title={watchedName}>
              {watchedName || t('knowledgeCompilation.unnamedTemplate')}
            </span>
          </button>
        </CollapsibleTrigger>
        <BuiltinTemplatePopover onSelect={handleApplyBuiltin} />
        {onRemove && (
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={onRemove}
            aria-label={t('common.delete')}
          >
            <Trash2 className="size-3.5" />
          </Button>
        )}
      </div>

      <CollapsibleContent className="p-4 pt-0 flex flex-col gap-4 border-t border-border-button">
        <FormField
          control={form.control}
          name={`${pathPrefix}.name` as any}
          render={({ field }) => (
            <FormItem>
              <FormLabel>{t('knowledgeCompilation.name')}</FormLabel>
              <FormControl>
                <Input
                  {...field}
                  maxLength={128}
                  placeholder={t('knowledgeCompilation.namePlaceholder')}
                />
              </FormControl>
              <FormMessage />
            </FormItem>
          )}
        />

        <FormField
          control={form.control}
          name={`${pathPrefix}.description` as any}
          render={({ field }) => (
            <FormItem>
              <FormLabel>{t('knowledgeCompilation.description')}</FormLabel>
              <FormControl>
                <Input
                  {...field}
                  maxLength={TEXT_FIELD_MAX}
                  placeholder={t('knowledgeCompilation.descriptionPlaceholder')}
                />
              </FormControl>
              <FormMessage />
            </FormItem>
          )}
        />

        {/* Kind is set when the user picks a built-in template and is
            not editable afterwards — it's surfaced in the header chip
            above. The form value still lives at ``${pathPrefix}.kind``
            so the conditional sections (Entity/Relation, RAPTOR,
            Page-structure example, etc.) render correctly. The
            ``allowedKinds`` prop is no longer consulted from this
            file but stays on the prop surface for future use. */}

        <FormField
          control={form.control}
          name={`${pathPrefix}.llm_id` as any}
          render={({ field }) => (
            <FormItem>
              <FormLabel>{t('knowledgeCompilation.llmLabel')}</FormLabel>
              <FormControl>
                <LLMSelect isEdit field={field} />
              </FormControl>
              <FormMessage />
            </FormItem>
          )}
        />

        {/* Page-structure example sits directly under LLM for artifacts
            so it's visible before the heavier Entity/Relation blocks. */}
        {watchedKind === 'artifacts' && (
          <FormField
            control={form.control}
            name={`${pathPrefix}.example` as any}
            render={({ field }) => (
              <FormItem>
                <FormLabel>{t('knowledgeCompilation.exampleLabel')}</FormLabel>
                <FormControl>
                  <Textarea
                    rows={10}
                    maxLength={8000}
                    className="font-mono text-sm"
                    placeholder={t('knowledgeCompilation.examplePlaceholder')}
                    value={field.value ?? ''}
                    onBlur={field.onBlur}
                    onChange={field.onChange}
                    name={field.name}
                    ref={field.ref}
                  />
                </FormControl>
                <p className="text-xs text-text-secondary">
                  {t('knowledgeCompilation.exampleDescription')}
                </p>
                <FormMessage />
              </FormItem>
            )}
          />
        )}

        {watchedKind !== 'tree' && watchedKind && (
          <>
            <EntityRelationSection
              variant="entity"
              kind={watchedKind}
              fieldTemplates={fieldTemplates.entity}
              pathPrefix={pathPrefix}
            />
            <EntityRelationSection
              variant="relation"
              kind={watchedKind}
              fieldTemplates={fieldTemplates.relation}
              pathPrefix={pathPrefix}
            />
          </>
        )}

        {watchedKind === 'artifacts' && (
          <ArtifactExtras pathPrefix={pathPrefix} />
        )}
        {watchedKind === 'tree' && <TreeExtras pathPrefix={pathPrefix} />}

        <GlobalRulesBlock pathPrefix={pathPrefix} />
      </CollapsibleContent>
    </Collapsible>
  );
}
