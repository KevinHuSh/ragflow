import message from '@/components/ui/message';
import {
  BuiltinCompilationTemplate,
  CompilationTemplateGroup,
  CompilationTemplateGroupListResponse,
} from '@/interfaces/database/compilation-template';
import {
  ICreateCompilationTemplateGroupRequest,
  IUpdateCompilationTemplateGroupRequest,
} from '@/interfaces/request/compilation-template';
import i18n from '@/locales/config';
import compilationTemplateService from '@/services/compilation-template-service';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useDebounce } from 'ahooks';
import {
  useGetPaginationWithRouter,
  useHandleSearchChange,
} from './logic-hooks';

/**
 * Query-key factory for the knowledge-compilation surface. Two domains
 * coexist:
 *   - ``group`` : the user-managed entity (CRUD, list, detail)
 *   - ``builtin``: the read-only template palette used as pre-fill
 *
 * Every ``useQuery`` and ``invalidateQueries`` in this file (or anywhere
 * else that touches this data) must go through this factory.
 */
export const CompilationTemplateKeys = {
  all: () => ['compilation_template'] as const,
  groups: () => ['compilation_template', 'group'] as const,
  groupList: (filters: { search?: string; page?: number; pageSize?: number }) =>
    ['compilation_template', 'group', 'list', filters] as const,
  groupDetail: (id: string) =>
    ['compilation_template', 'group', 'detail', id] as const,
  builtins: () => ['compilation_template', 'builtins'] as const,
};

export const useListCompilationTemplateGroups = () => {
  const { searchString, handleInputChange } = useHandleSearchChange();
  const { pagination, setPagination } = useGetPaginationWithRouter();
  const debouncedSearchString = useDebounce(searchString, { wait: 500 });

  const { data, isFetching: loading } =
    useQuery<CompilationTemplateGroupListResponse>({
      queryKey: CompilationTemplateKeys.groupList({
        search: debouncedSearchString,
        page: pagination.current,
        pageSize: pagination.pageSize,
      }),
      initialData: { total: 0, groups: [] },
      gcTime: 0,
      queryFn: async () => {
        const { data } = await compilationTemplateService.listGroups({
          keywords: debouncedSearchString,
          page: pagination.current,
          page_size: pagination.pageSize,
        });
        return data?.data ?? { total: 0, groups: [] };
      },
    });

  return {
    data,
    loading,
    handleInputChange,
    setPagination,
    searchString,
    pagination: { ...pagination, total: data?.total },
  };
};

/**
 * Fetch *all* saved groups for the tenant — used by the dataset
 * parser-config picker and anywhere else that needs the full list
 * without paging.
 */
export const useFetchSavedCompilationTemplateGroups = () => {
  const { data, isFetching: loading } =
    useQuery<CompilationTemplateGroupListResponse>({
      queryKey: CompilationTemplateKeys.groupList({
        search: '',
        page: 1,
        pageSize: 100,
      }),
      initialData: { total: 0, groups: [] },
      queryFn: async () => {
        const { data } = await compilationTemplateService.listGroups({
          keywords: '',
          page: 1,
          page_size: 100,
        });
        return data?.data ?? { total: 0, groups: [] };
      },
    });

  return { data, loading };
};

export const useFetchCompilationTemplateGroup = (id: string) => {
  const { data, isFetching: loading } = useQuery<
    CompilationTemplateGroup | undefined
  >({
    queryKey: CompilationTemplateKeys.groupDetail(id),
    initialData: undefined,
    gcTime: 0,
    enabled: !!id,
    queryFn: async () => {
      const { data } = await compilationTemplateService.getGroup({ id });
      return data?.data;
    },
  });

  return { data, loading, id };
};

/**
 * Cached server-side defaults. Stable across the session — the same
 * factory entry is reused by the editor popover and the seeding helpers.
 */
export const useFetchBuiltinCompilationTemplates = () => {
  const { data, isFetching, isLoading, refetch } = useQuery<
    BuiltinCompilationTemplate[]
  >({
    queryKey: CompilationTemplateKeys.builtins(),
    staleTime: 0,
    refetchOnMount: 'always',
    queryFn: async () => {
      const { data } = await compilationTemplateService.builtins();
      return [...(data?.data ?? [])].sort((a, b) => {
        if (a.kind === 'empty' && b.kind !== 'empty') return 1;
        if (a.kind !== 'empty' && b.kind === 'empty') return -1;
        return a.display_name.localeCompare(b.display_name);
      });
    },
  });

  return { data: data ?? [], loading: isLoading || isFetching, refetch };
};

export const useCreateCompilationTemplateGroup = () => {
  const queryClient = useQueryClient();
  const {
    data,
    isPending: loading,
    mutateAsync,
  } = useMutation({
    mutationKey: ['createCompilationTemplateGroup'],
    mutationFn: async (params: ICreateCompilationTemplateGroupRequest) => {
      const { data = {} } =
        await compilationTemplateService.createGroup(params);
      if (data.code === 0) {
        message.success(i18n.t('message.created'));
        queryClient.invalidateQueries({
          queryKey: CompilationTemplateKeys.groups(),
        });
      }
      return data.code;
    },
  });

  return { data, loading, createCompilationTemplateGroup: mutateAsync };
};

export const useUpdateCompilationTemplateGroup = () => {
  const queryClient = useQueryClient();
  const {
    data,
    isPending: loading,
    mutateAsync,
  } = useMutation({
    mutationKey: ['updateCompilationTemplateGroup'],
    mutationFn: async (params: IUpdateCompilationTemplateGroupRequest) => {
      const { data = {} } =
        await compilationTemplateService.updateGroup(params);
      if (data.code === 0) {
        message.success(i18n.t('message.updated'));
        queryClient.invalidateQueries({
          queryKey: CompilationTemplateKeys.groups(),
        });
      }
      return data.code;
    },
  });

  return { data, loading, updateCompilationTemplateGroup: mutateAsync };
};

export const useDeleteCompilationTemplateGroup = () => {
  const queryClient = useQueryClient();
  const {
    data,
    isPending: loading,
    mutateAsync,
  } = useMutation({
    mutationKey: ['deleteCompilationTemplateGroup'],
    mutationFn: async (ids: string[]) => {
      const results = await Promise.all(
        ids.map((id) => compilationTemplateService.deleteGroup({ id })),
      );
      const failed = results.find(({ data = {} }) => data.code !== 0);
      const data = failed?.data ?? { code: 0, data: true };
      if (!failed) {
        message.success(i18n.t('message.deleted'));
        queryClient.invalidateQueries({
          queryKey: CompilationTemplateKeys.groups(),
        });
      }
      return data;
    },
  });

  return { data, loading, deleteCompilationTemplateGroup: mutateAsync };
};
