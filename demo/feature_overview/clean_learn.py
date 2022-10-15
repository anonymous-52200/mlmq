"""
The Cleanlearn What-If Analysis for the running example
"""
# pylint: disable-all
import dataclasses
from enum import Enum
from functools import partial
from typing import Iterable, Dict, Callable

import networkx
import pandas

from mlmq import OperatorType, DagNode, OperatorContext, DagNodeDetails, BasicCodeLocation
from mlmq.analysis._analysis_utils import find_nodes_by_type, get_columns_used_as_feature
from mlmq.analysis._patch_creation import get_intermediate_extraction_patch_after_score_nodes
from mlmq.analysis._what_if_analysis import WhatIfAnalysis
from mlmq.execution._patches import DataFiltering, PipelinePatch, DataProjection
from mlmq.execution._pipeline_executor import singleton


class ErrorType(Enum):
    """
    The different error types supported by the data cleaning what-if analysis
    """
    OUTLIER = "outliers"


class Clean(Enum):
    FILTER = "filter"
    IMPUTE = "impute"


class PatchType(Enum):
    """
    The different patch types
    """
    DATA_FILTER_PATCH = "data filter patch"
    DATA_ESTIMATOR_PATCH = "transformer patch"


@dataclasses.dataclass
class CleaningMethod:
    """
    A DAG Node
    """

    method_name: str
    patch_type: PatchType
    filter_func: Callable or None = None
    fit_or_fit_transform_func: Callable or None = None
    predict_or_fit_func: Callable or None = None
    numeric_only: bool = False
    categorical_only: bool = False

    def __hash__(self):
        return hash(self.method_name)


def drop_outliers(input_df, column, outlier_func):
    """Drop rows with missing values in that column"""
    df_copy = input_df.copy()
    indexes = df_copy[column].loc[outlier_func].index
    result = df_copy.drop(indexes)
    return result


def impute_outliers(input_df, column, constant, outlier_func):
    """Drop rows with missing values in that column"""
    df_copy = input_df.copy()
    indexes = df_copy[column].loc[outlier_func].index
    df_copy.loc[indexes, column] = constant
    return df_copy


class CleanLearn(WhatIfAnalysis):
    """
    The Data Cleaning What-If Analysis
    """

    def __init__(self, column, error, cleanings, impute_constant, outlier_func):
        self.column = column
        self.error = error
        self.cleanings = cleanings
        self._score_nodes_and_linenos = []
        self.impute_constant = impute_constant
        self.outlier_func = outlier_func
        self._analysis_id = (self.column, self.error, self.impute_constant, *self.cleanings,)

    @property
    def analysis_id(self):
        return self._analysis_id

    def generate_plans_to_try(self, dag: networkx.DiGraph) -> Iterable[Iterable[PipelinePatch]]:
        # pylint: disable=too-many-locals,too-many-branches,too-many-statements
        predict_operators = find_nodes_by_type(dag, OperatorType.PREDICT)
        if len(predict_operators) != 1:
            raise Exception("Currently, DataCorruption only supports pipelines with exactly one predict call which "
                            "must be on the test set!")
        score_operators = find_nodes_by_type(dag, OperatorType.SCORE)
        self._score_nodes_and_linenos = [(node, node.code_location.lineno) for node in score_operators]
        if len(self._score_nodes_and_linenos) != len(set(self._score_nodes_and_linenos)):
            raise Exception("Currently, DataCorruption only supports pipelines where different score operations can "
                            "be uniquely identified by the line number in the code!")
        if self.error != ErrorType.OUTLIER:
            raise Exception("Please use the actual DataCleaning analysis instead of this more simple one"
                            " for the running example!")
        cleaning_patch_sets = []
        for cleaning in self.cleanings:
            if cleaning == Clean.FILTER:
                cleaning_result_label = f"data-cleaning-{self.column}-{cleaning.value}"
                patches_for_variant = []
                extraction_nodes = get_intermediate_extraction_patch_after_score_nodes(singleton, self,
                                                                                       cleaning_result_label,
                                                                                       self._score_nodes_and_linenos)
                patches_for_variant.extend(extraction_nodes)
                feature_cols = set(get_columns_used_as_feature(dag))
                if self.column in feature_cols:
                    required_cols = list(feature_cols)
                else:
                    required_cols = [self.column]
                filter_func = partial(drop_outliers, column=self.column, outlier_func=self.outlier_func)

                new_test_cleaning_node = DagNode(singleton.get_next_op_id(),
                                                  BasicCodeLocation("Data Cleaning", None),
                                                  OperatorContext(OperatorType.SELECTION, None),
                                                  DagNodeDetails(
                                                      f"Clean {self.column}: filter", None),
                                                  None,
                                                  filter_func)
                filter_patch_train = DataFiltering(singleton.get_next_patch_id(), self, True,
                                                   new_test_cleaning_node, False, required_cols)
                patches_for_variant.append(filter_patch_train)

            elif cleaning == Clean.IMPUTE:
                cleaning_result_label = f"data-cleaning-{self.column}-{cleaning.value}"
                patches_for_variant = []
                extraction_nodes = get_intermediate_extraction_patch_after_score_nodes(singleton, self,
                                                                                       cleaning_result_label,
                                                                                       self._score_nodes_and_linenos)
                patches_for_variant.extend(extraction_nodes)
                only_reads_column = [self.column]
                projection = partial(impute_outliers, column=self.column, constant=self.impute_constant,
                                     outlier_func=self.outlier_func)
                new_projection_node = DagNode(singleton.get_next_op_id(),
                                              BasicCodeLocation("DataCorruption", None),
                                              OperatorContext(OperatorType.PROJECTION_MODIFY, None),
                                              DagNodeDetails(f"Clean {self.column}: impute", None),
                                              None,
                                              projection)
                patch = DataProjection(singleton.get_next_patch_id(), self, True, new_projection_node, False, self.column,
                                       only_reads_column, None)
                patches_for_variant.append(patch)
            else:
                raise Exception("Unknown cleaning. Please use the real, more complex DataCleaning what-if analysis.")

            cleaning_patch_sets.append(patches_for_variant)
        return cleaning_patch_sets

    def generate_final_report(self, extracted_plan_results: Dict[str, any]) -> any:
        # pylint: disable=too-many-locals
        result_df_columns = []
        result_df_errors = []
        result_df_cleaning_methods = []
        result_df_metrics = {}
        score_description_and_linenos = [(score_node.details.description, lineno)
                                         for (score_node, lineno) in self._score_nodes_and_linenos]

        result_df_columns.append(None)
        result_df_errors.append(None)
        result_df_cleaning_methods.append(None)
        for (score_description, lineno) in score_description_and_linenos:
            original_pipeline_result_label = f"original_L{lineno}"
            test_result_column_name = f"{score_description}_L{lineno}"
            test_column_values = result_df_metrics.get(test_result_column_name, [])
            test_column_values.append(singleton.labels_to_extracted_plan_results[original_pipeline_result_label])
            result_df_metrics[test_result_column_name] = test_column_values

        for (column, error_type) in [(self.column, self.error)]:
            for cleaning in self.cleanings:
                result_df_columns.append(column)
                result_df_errors.append(error_type.value)
                result_df_cleaning_methods.append(cleaning.value)
                for (score_description, lineno) in score_description_and_linenos:
                    cleaning_result_label = f"data-cleaning-{column}-{cleaning.value}_L{lineno}"
                    test_result_column_name = f"{score_description}_L{lineno}"
                    test_column_values = result_df_metrics.get(test_result_column_name, [])
                    test_column_values.append(singleton.labels_to_extracted_plan_results[cleaning_result_label])
                    result_df_metrics[test_result_column_name] = test_column_values
        result_df = pandas.DataFrame({'corrupted_column': result_df_columns,
                                      'error': result_df_errors,
                                      'cleaning_method': result_df_cleaning_methods,
                                      **result_df_metrics})
        return result_df
