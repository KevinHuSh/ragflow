import { useIsDarkTheme } from '@/components/theme-provider';
import { Button } from '@/components/ui/button';
import {
  Form,
  FormControl,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from '@/components/ui/form';
import { Input } from '@/components/ui/input';
import message from '@/components/ui/message';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { useFetchDefaultModelDictionary } from '@/hooks/use-llm-request';
import {
  COMPILATION_TEMPLATE_KINDS,
  CompilationTemplateGroup,
  CompilationTemplateKind,
} from '@/interfaces/database/compilation-template';
import { zodResolver } from '@hookform/resolvers/zod';
import { Plus } from 'lucide-react';
import { useCallback, useEffect, useMemo } from 'react';
import {
  FormProvider,
  useFieldArray,
  useForm,
  useWatch,
} from 'react-hook-form';
import { useTranslation } from 'react-i18next';
import JsonView from 'react18-json-view';
import 'react18-json-view/src/dark.css';
import 'react18-json-view/src/style.css';
import { ChildFormSection } from './components/child-form-section';
import {
  compilationTemplateGroupFormSchema,
  CompilationTemplateGroupFormValues,
  deriveGroupScope,
  emptyGroupFormValues,
  groupFormValuesToPayload,
  groupToFormValues,
} from './group-interface';
import { emptyFormValues, TEXT_FIELD_MAX } from './interface';

/**
 * Walk RHF's nested ``FieldErrors`` tree and return the first
 * ``message`` string we find. Same helper as in the single-template
 * form; duplicated here to keep the file self-contained while we
 * decide whether to extract it.
 */
function _firstFormErrorMessage(errors: unknown): string | undefined {
  if (!errors || typeof errors !== 'object') return undefined;
  const stack: unknown[] = [errors];
  while (stack.length) {
    const node = stack.pop();
    if (!node || typeof node !== 'object') continue;
    const rec = node as Record<string, unknown>;
    if (typeof rec.message === 'string' && rec.message) {
      return rec.message;
    }
    for (const value of Object.values(rec)) {
      if (value && typeof value === 'object') {
        stack.push(value);
      }
    }
  }
  return undefined;
}

interface EditTemplateGroupFormProps {
  initial?: CompilationTemplateGroup;
  savedGroups?: CompilationTemplateGroup[];
  onSubmit: (
    payload: ReturnType<typeof groupFormValuesToPayload>,
  ) => Promise<void> | void;
  onCancel: () => void;
  onDirtyChange?: (dirty: boolean) => void;
  loading?: boolean;
}

export function EditTemplateGroupForm({
  initial,
  savedGroups = [],
  onSubmit,
  onCancel,
  onDirtyChange,
  loading,
}: EditTemplateGroupFormProps) {
  const { t } = useTranslation();
  const defaultModelDictionary = useFetchDefaultModelDictionary();
  const defaultChatLlmId = defaultModelDictionary.llm_id;

  const defaultValues = useMemo<CompilationTemplateGroupFormValues>(() => {
    if (initial) return groupToFormValues(initial);
    return emptyGroupFormValues();
  }, [initial]);

  const form = useForm<CompilationTemplateGroupFormValues>({
    resolver: zodResolver(compilationTemplateGroupFormSchema),
    defaultValues,
  });

  useEffect(() => {
    form.reset(defaultValues);
  }, [defaultValues, form]);

  useEffect(() => {
    if (!defaultChatLlmId) return;
    const templates = form.getValues('templates') ?? [];
    templates.forEach((template, index) => {
      if (!template?.llm_id) {
        form.setValue(`templates.${index}.llm_id`, defaultChatLlmId, {
          shouldDirty: false,
          shouldValidate: true,
        });
      }
    });
  }, [defaultChatLlmId, defaultValues, form]);

  useEffect(() => {
    onDirtyChange?.(form.formState.isDirty);
  }, [form.formState.isDirty, onDirtyChange]);

  const childrenArray = useFieldArray({
    control: form.control,
    name: 'templates',
  });

  const isDark = useIsDarkTheme();

  // Watching the kinds drives both the scope badge and the mutual
  // exclusion rule applied to the per-child kind picker.
  const watchedTemplates = useWatch({
    control: form.control,
    name: 'templates',
  }) as CompilationTemplateGroupFormValues['templates'] | undefined;

  // Build the live JSON preview from the currently-watched form
  // values. The shape matches what gets POSTed on save (run through
  // ``groupFormValuesToPayload``) so the user sees exactly the
  // request body their next "Save" click will produce. Wrapped in
  // ``useMemo`` so the JsonView only re-keyed when the underlying
  // values actually change.
  const previewTemplates = useMemo(() => {
    try {
      const payload = groupFormValuesToPayload({
        name: form.getValues('name') ?? '',
        description: form.getValues('description') ?? '',
        templates: watchedTemplates ?? [],
      } as CompilationTemplateGroupFormValues);
      return payload.templates;
    } catch {
      return watchedTemplates ?? [];
    }
  }, [watchedTemplates, form]);

  const watchedKinds = (watchedTemplates ?? []).map((t) => t.kind);
  const hasArtifacts = watchedKinds.includes('artifacts');
  const hasNonArtifacts = watchedKinds.some((k) => k && k !== 'artifacts');
  const scope = deriveGroupScope(watchedTemplates ?? []);

  const allowedKindsFor = useCallback(
    (currentKind: CompilationTemplateKind): CompilationTemplateKind[] => {
      // Always allow the kind this child already has, so the picker
      // shows its current value selected even when the rule would
      // otherwise disable it (covers the moment the user opens the
      // panel before changing anything).
      return COMPILATION_TEMPLATE_KINDS.filter((k) => {
        if (k === currentKind) return true;
        if (k === 'artifacts') return !hasNonArtifacts;
        return !hasArtifacts;
      });
    },
    [hasArtifacts, hasNonArtifacts],
  );

  const handleAdd = useCallback(() => {
    const next = emptyFormValues();
    next.llm_id = defaultChatLlmId || '';
    // The newcomer's kind defaults to ``empty`` — keep it that way so
    // the picker offers every option (it'll be greyed appropriately
    // once the user makes a selection that conflicts with siblings).
    childrenArray.append(next);
  }, [childrenArray, defaultChatLlmId]);

  const canAddArtifacts = !hasNonArtifacts && !hasArtifacts;
  const canAddOthers = !hasArtifacts;
  const addDisabled = !canAddArtifacts && !canAddOthers;

  const handleSubmit = form.handleSubmit(
    async (values) => {
      const normalizedName = values.name.trim();
      const dup = savedGroups.some(
        (g) =>
          g.id !== initial?.id &&
          g.name.trim().toLowerCase() === normalizedName.toLowerCase(),
      );
      if (dup) {
        form.setError('name', {
          type: 'validate',
          message: t('knowledgeCompilation.nameDuplicated'),
        });
        return;
      }
      await onSubmit(groupFormValuesToPayload(values));
    },
    (errors) => {
      const firstError = _firstFormErrorMessage(errors);
      message.error(firstError ?? t('knowledgeCompilation.formInvalid'));
    },
  );

  return (
    <FormProvider {...form}>
      <Form {...form}>
        <form onSubmit={handleSubmit} className="flex flex-col gap-4">
          <FormField
            control={form.control}
            name="name"
            render={({ field }) => (
              <FormItem>
                <FormLabel>{t('knowledgeCompilation.groupName')}</FormLabel>
                <FormControl>
                  <Input
                    {...field}
                    maxLength={128}
                    placeholder={t('knowledgeCompilation.groupNamePlaceholder')}
                    autoFocus
                  />
                </FormControl>
                <FormMessage />
              </FormItem>
            )}
          />

          <FormField
            control={form.control}
            name="description"
            render={({ field }) => (
              <FormItem>
                <FormLabel>{t('knowledgeCompilation.description')}</FormLabel>
                <FormControl>
                  <Input
                    {...field}
                    maxLength={TEXT_FIELD_MAX}
                    placeholder={t(
                      'knowledgeCompilation.descriptionPlaceholder',
                    )}
                  />
                </FormControl>
                <FormMessage />
              </FormItem>
            )}
          />

          <div className="flex items-center gap-2 text-xs text-text-secondary">
            <span>{t('knowledgeCompilation.groupScopeLabel')}:</span>
            <span className="font-medium uppercase">
              {t(`knowledgeCompilation.scope.${scope}`)}
            </span>
          </div>

          {/* Editor / Raw JSON tabs. The form children stay mounted
              inside the "editor" tab so RHF doesn't tear down each
              child's RHF state when the user toggles to JSON. The
              "json" tab renders a live, highlighted view of the
              outgoing payload's ``templates`` array. */}
          <Tabs defaultValue="editor" className="w-full">
            <TabsList>
              <TabsTrigger value="editor">
                {t('knowledgeCompilation.editorTab')}
              </TabsTrigger>
              <TabsTrigger value="json">
                {t('knowledgeCompilation.rawJsonTab')}
              </TabsTrigger>
            </TabsList>

            <TabsContent value="editor">
              <div className="flex flex-col gap-3">
                {childrenArray.fields.map((field, index) => (
                  <ChildFormSection
                    key={field.id}
                    index={index}
                    pathPrefix={`templates.${index}`}
                    onRemove={
                      childrenArray.fields.length > 1
                        ? () => childrenArray.remove(index)
                        : undefined
                    }
                    allowedKinds={allowedKindsFor(
                      (watchedTemplates?.[index]?.kind ??
                        'empty') as CompilationTemplateKind,
                    )}
                  />
                ))}
                <Button
                  type="button"
                  variant="outline"
                  onClick={handleAdd}
                  disabled={addDisabled}
                  className="self-start"
                >
                  <Plus className="size-3.5" />
                  {t('knowledgeCompilation.addChildTemplate')}
                </Button>
              </div>
            </TabsContent>

            <TabsContent value="json">
              <div className="rounded-md border border-border-button bg-bg-base p-4 max-h-[60vh] overflow-auto font-mono text-xs">
                <JsonView
                  src={previewTemplates}
                  dark={isDark}
                  collapsed={2}
                  enableClipboard
                  displayObjectSize={false}
                  displayDataTypes={false}
                />
              </div>
            </TabsContent>
          </Tabs>

          <div className="flex justify-end gap-2 pt-2 border-t border-border-button">
            <Button type="button" variant="ghost" onClick={onCancel}>
              {t('common.cancel')}
            </Button>
            <Button type="submit" disabled={loading}>
              {t('common.save')}
            </Button>
          </div>
        </form>
      </Form>
    </FormProvider>
  );
}
