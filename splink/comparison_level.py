from __future__ import annotations

import logging
import math
import re
from statistics import median
from textwrap import dedent
from typing import TYPE_CHECKING

import sqlglot
from sqlglot.expressions import Identifier
from sqlglot.optimizer.normalize import normalize
from sqlglot.optimizer.simplify import simplify

from .constants import LEVEL_NOT_OBSERVED_TEXT
from .input_column import InputColumn
from .misc import (
    dedupe_preserving_order,
    interpolate,
    join_list_with_commas_final_and,
    match_weight_to_bayes_factor,
)
from .parse_sql import get_columns_used_from_sql
from .sql_transform import sqlglot_tree_signature

# https://stackoverflow.com/questions/39740632/python-type-hinting-without-cyclic-imports
if TYPE_CHECKING:
    from .comparison import Comparison

logger = logging.getLogger(__name__)


def _is_exact_match(sql_syntax_tree):
    signature = sqlglot_tree_signature(sql_syntax_tree)

    if signature != sqlglot_tree_signature(sqlglot.parse_one("col_l = col_r")):
        return False

    identifiers = []
    for tup in sql_syntax_tree.walk():
        subtree = tup[0]
        if type(subtree) is Identifier:
            identifiers.append(subtree.this[:-2])
    if identifiers[0] == identifiers[1]:
        return True
    else:
        return False


def _exact_match_colname(sql_syntax_tree):
    # only interested in expression directly, not context
    sql_syntax_tree.parent = None
    cols = []

    for identifier in sql_syntax_tree.find_all(Identifier):
        identifier.args["quoted"] = False

    for tup in sql_syntax_tree.walk():
        subtree = tup[0]
        depth = getattr(subtree, "depth", None)
        if depth == 2:
            cols.append(subtree.sql())

    cols = [c[:-2] for c in cols]  # Remove _l and _r
    cols = list(set(cols))
    if len(cols) != 1:
        raise ValueError(
            f"Expected sql condition to refer to one column but got {cols}"
        )
    return cols[0]


def _get_and_subclauses(expr: sqlglot.Expression):
    # get list of subclauses joined together by 'AND' at top-level
    # e.g. 'A AND B AND C' -> ['A', 'B', 'C']
    # or if no AND, return expression as a list, e.g. 'A' -> ['A']
    if isinstance(expr, sqlglot.exp.And):
        return list(expr.flatten())
    return [expr]


def _default_m_values(num_levels):
    proportion_exact_match = 0.95
    remainder = 1 - proportion_exact_match
    split_remainder = remainder / (num_levels - 1)
    return [split_remainder] * (num_levels - 1) + [proportion_exact_match]


def _default_u_values(num_levels):
    m_vals = _default_m_values(num_levels)
    if num_levels == 2:
        match_weights = [-5]
    else:
        match_weights = interpolate(-5, 3, num_levels - 1)
    match_weights = match_weights + [10]

    u_vals = []
    for m, w in zip(m_vals, match_weights):
        p = match_weight_to_bayes_factor(w)
        u = m / p
        u_vals.append(u)

    return u_vals


class ComparisonLevel:
    """Each ComparisonLevel defines a gradation (category) of similarity within a
    `Comparison`.

    For example, a `Comparison` that uses the first_name and surname columns may
    define three `ComparisonLevel`s:
        An exact match on first name and surname
        First name and surname have a JaroWinkler score of above 0.95
        All other comparisons

    The method used to assess similarity will depend on the type of data - for
    instance, the method used to assess similarity of a company's turnover would be
    different to the method used to assess the similarity of a person's first name.

    To summarise:

    ```
    Data Linking Model
    ├─-- Comparison: Name
    │    ├─-- ComparisonLevel: Exact match on first_name and surname
    │    ├─-- ComparisonLevel: first_name and surname have JaroWinkler > 0.95
    │    ├─-- ComparisonLevel: All other
    ├─-- Comparison: Date of birth
    │    ├─-- ComparisonLevel: Exact match
    │    ├─-- ComparisonLevel: One character difference
    │    ├─-- ComparisonLevel: All other
    ├─-- etc.
    ```

    ComparisonLevel is a dialected object.
    """

    def __init__(
        self,
        sql_condition: str,
        # TODO: do we want dialect or just dialect name?
        sqlglot_dialect_name: str,
        *,
        label_for_charts: str = None,
        is_null_level: bool = False,
        tf_adjustment_column: str = None,
        tf_adjustment_weight: float = 1.0,
        tf_minimum_u_value: float = 0.0,
        m_probability: float = None,
        u_probability: float = None,
        comparison: Comparison = None,
    ):
        self.comparison: Comparison = comparison
        self._sqlglot_dialect_name = sqlglot_dialect_name

        self._sql_condition = sql_condition
        self._is_null_level = is_null_level
        self._label_for_charts = label_for_charts

        self._tf_adjustment_column = tf_adjustment_column
        self._tf_adjustment_weight = tf_adjustment_weight
        self._tf_minimum_u_value = tf_minimum_u_value

        self._m_probability = m_probability
        self._u_probability = u_probability

        # TODO: control this in comparison getter setter ?
        # These will be set when the ComparisonLevel is passed into a Comparison
        self._comparison_vector_value: int = None
        self._max_level: bool = None

        # Enable the level to 'know' when it's been trained
        self._trained_m_probabilities: list = []
        self._trained_u_probabilities: list = []
        # controls warnings from model training - ensures we only send once
        self._m_warning_sent = False
        self._u_warning_sent = False

        self._validate()

    @property
    def sql_dialect(self):
        # TODO: align name with attribute
        return self._sqlglot_dialect_name

    @property
    def is_null_level(self) -> bool:
        return self._is_null_level

    @property
    def sql_condition(self) -> str:
        return self._sql_condition

    @property
    def _tf_adjustment_input_column(self):
        val = self._tf_adjustment_column
        if val:
            return InputColumn(val, sql_dialect=self.sql_dialect)
        else:
            return None

    @property
    def _tf_adjustment_input_column_name(self):
        input_column = self._tf_adjustment_input_column
        if input_column:
            return input_column.unquote().name

    @property
    def _has_comparison(self):
        from .comparison import Comparison

        return isinstance(self.comparison, Comparison)

    @property
    def m_probability(self):
        if self.is_null_level:
            return None
        if self._m_probability == LEVEL_NOT_OBSERVED_TEXT:
            return 1e-6
        if self._m_probability is None and self._has_comparison:
            vals = _default_m_values(self.comparison._num_levels)
            return vals[self._comparison_vector_value]
        return self._m_probability

    @m_probability.setter
    def m_probability(self, value):
        if self.is_null_level:
            raise AttributeError("Cannot set m_probability when is_null_level is true")
        if value == LEVEL_NOT_OBSERVED_TEXT:
            cc_n = self.comparison.output_column_name
            cl_n = self.label_for_charts
            if not self._m_warning_sent:
                logger.warning(
                    "WARNING:\n"
                    f"Level {cl_n} on comparison {cc_n} not observed in dataset, "
                    "unable to train m value\n"
                )
                self._m_warning_sent = True

        self._m_probability = value

    @property
    def u_probability(self):
        if self.is_null_level:
            return None
        if self._u_probability == LEVEL_NOT_OBSERVED_TEXT:
            return 1e-6
        if self._u_probability is None:
            vals = _default_u_values(self.comparison._num_levels)
            return vals[self._comparison_vector_value]
        return self._u_probability

    @u_probability.setter
    def u_probability(self, value):
        if self.is_null_level:
            raise AttributeError("Cannot set u_probability when is_null_level is true")
        if value == LEVEL_NOT_OBSERVED_TEXT:
            cc_n = self.comparison.output_column_name
            cl_n = self.label_for_charts
            if not self._u_warning_sent:
                logger.warning(
                    "WARNING:\n"
                    f"Level {cl_n} on comparison {cc_n} not observed in dataset, "
                    "unable to train u value\n"
                )
                self._u_warning_sent = True
        self._u_probability = value

    @property
    def _m_probability_description(self):
        if self.m_probability is not None:
            return (
                "Amongst matching record comparisons, "
                f"{self.m_probability:.2%} of records are in the "
                f"{self.label_for_charts.lower()} comparison level"
            )

    @property
    def _u_probability_description(self):
        if self.u_probability is not None:
            return (
                "Amongst non-matching record comparisons, "
                f"{self.u_probability:.2%} of records are in the "
                f"{self.label_for_charts.lower()} comparison level"
            )

    def _add_trained_u_probability(self, val, desc="no description given"):
        self._trained_u_probabilities.append(
            {"probability": val, "description": desc, "m_or_u": "u"}
        )

    def _add_trained_m_probability(self, val, desc="no description given"):
        self._trained_m_probabilities.append(
            {"probability": val, "description": desc, "m_or_u": "m"}
        )

    @property
    def _has_estimated_u_values(self):
        if self.is_null_level:
            return True
        vals = [r["probability"] for r in self._trained_u_probabilities]
        vals = [v for v in vals if isinstance(v, (int, float))]
        return len(vals) > 0

    @property
    def _has_estimated_m_values(self):
        if self.is_null_level:
            return True
        vals = [r["probability"] for r in self._trained_m_probabilities]
        vals = [v for v in vals if isinstance(v, (int, float))]
        return len(vals) > 0

    @property
    def _has_estimated_values(self):
        return self._has_estimated_m_values and self._has_estimated_u_values

    @property
    def _trained_m_median(self):
        vals = [r["probability"] for r in self._trained_m_probabilities]
        vals = [v for v in vals if isinstance(v, (int, float))]
        if len(vals) == 0:
            return None
        return median(vals)

    @property
    def _trained_u_median(self):
        vals = [r["probability"] for r in self._trained_u_probabilities]
        vals = [v for v in vals if isinstance(v, (int, float))]
        if len(vals) == 0:
            return None
        return median(vals)

    @property
    def _m_is_trained(self):
        if self.is_null_level:
            return True
        if self._m_probability == LEVEL_NOT_OBSERVED_TEXT:
            return False
        if self._m_probability is None:
            return False
        return True

    @property
    def _u_is_trained(self):
        if self.is_null_level:
            return True
        if self._u_probability == LEVEL_NOT_OBSERVED_TEXT:
            return False
        if self._u_probability is None:
            return False
        return True

    @property
    def _is_trained(self):
        return self._m_is_trained and self._u_is_trained

    @property
    def _bayes_factor(self):
        if self.is_null_level:
            return 1.0
        if self.m_probability is None or self.u_probability is None:
            return None
        elif self.u_probability == 0:
            return math.inf
        else:
            return self.m_probability / self.u_probability

    @property
    def _log2_bayes_factor(self):
        if self.is_null_level:
            return 0.0
        else:
            return math.log2(self._bayes_factor)

    @property
    def _bayes_factor_description(self):
        text = (
            f"If comparison level is `{self.label_for_charts.lower()}` "
            "then comparison is"
        )
        if self._bayes_factor == math.inf:
            return f"{text} certain to be a match"
        elif self._bayes_factor == 0.0:
            return f"{text} impossible to be a match"
        elif self._bayes_factor >= 1.0:
            return f"{text} {self._bayes_factor:,.2f} times more likely to be a match"
        else:
            mult = 1 / self._bayes_factor
            return f"{text}  {mult:,.2f} times less likely to be a match"

    @property
    def label_for_charts(self):
        return self._label_for_charts or str(self._comparison_vector_value)

    def _label_for_charts_no_duplicates(self, comparison_levels: list[ComparisonLevel]):
        if self._has_comparison:
            labels = []
            for cl in comparison_levels:
                labels.append(cl.label_for_charts)

        if len(labels) == len(set(labels)):
            return self.label_for_charts

        # Make label unique
        cvv = str(self._comparison_vector_value)
        label = self.label_for_charts
        return f"{cvv}. {label}"

    @property
    def _is_else_level(self):
        if self.sql_condition.strip().upper() == "ELSE":
            return True

    @property
    def _has_tf_adjustments(self):
        col = self._tf_adjustment_column
        return col is not None

    def _validate_sql(self):
        sql = self.sql_condition
        if self._is_else_level:
            return True
        dialect = self.sql_dialect
        try:
            sqlglot.parse_one(sql, read=dialect)
        except sqlglot.ParseError as e:
            raise ValueError(f"Error parsing sql_statement:\n{sql}") from e

        return True

    @property
    def _input_columns_used_by_sql_condition(self) -> list[InputColumn]:
        # returns e.g. InputColumn(first_name), InputColumn(surname)

        if self._is_else_level:
            return []

        cols = get_columns_used_from_sql(self.sql_condition, dialect=self.sql_dialect)
        # Parsed order seems to be roughly in reverse order of apearance
        cols = cols[::-1]

        cols = [re.sub(r"_L$|_R$", "", c, flags=re.IGNORECASE) for c in cols]
        cols = dedupe_preserving_order(cols)

        input_cols = []
        for c in cols:
            # We could have tf adjustments for surname on a dmeta_surname column
            # If so, we want to set the tf adjustments against the surname col,
            # not the dmeta_surname one

            input_cols.append(InputColumn(c, sql_dialect=self.sql_dialect))

        return input_cols

    @property
    def _columns_to_select_for_blocking(self):
        # e.g. l.first_name as first_name_l, r.first_name as first_name_r
        output_cols = []
        cols = self._input_columns_used_by_sql_condition

        for c in cols:
            output_cols.extend(c.l_r_names_as_l_r)
            if self._tf_adjustment_input_column:
                output_cols.extend(self._tf_adjustment_input_column.l_r_tf_names_as_l_r)

        return dedupe_preserving_order(output_cols)

    @property
    def _when_then_comparison_vector_value_sql(self):
        # e.g. when first_name_l = first_name_r then 1
        if not hasattr(self, "_comparison_vector_value"):
            raise ValueError(
                "Cannot get the 'when .. then ...' sql expression because "
                "this comparison level does not belong to a parent Comparison. "
                "The comparison_vector_value is only defined in the "
                "context of a list of ComparisonLevels within a Comparison."
            )
        if self._is_else_level:
            return f"{self.sql_condition} {self._comparison_vector_value}"
        else:
            return f"WHEN {self.sql_condition} THEN {self._comparison_vector_value}"

    @property
    def _is_exact_match(self):
        if self._is_else_level:
            return False

        sql_syntax_tree = sqlglot.parse_one(
            self.sql_condition.lower(), read=self.sql_dialect
        )
        sql_cnf = simplify(normalize(sql_syntax_tree))

        exprs = _get_and_subclauses(sql_cnf)
        for expr in exprs:
            if not _is_exact_match(expr):
                return False
        return True

    @property
    def _exact_match_colnames(self):
        sql_syntax_tree = sqlglot.parse_one(
            self.sql_condition.lower(), read=self.sql_dialect
        )
        sql_cnf = simplify(normalize(sql_syntax_tree))

        exprs = _get_and_subclauses(sql_cnf)
        for expr in exprs:
            if not _is_exact_match(expr):
                raise ValueError(
                    "sql_cond not an exact match so can't get exact match column name"
                )

        cols = []
        for expr in exprs:
            col = _exact_match_colname(expr)
            cols.append(col)
        return cols

    def _u_probability_corresponding_to_exact_match(
        self, comparison_levels: list[ComparisonLevel]
    ):
        # Find a level with a single exact match colname
        # which is equal to the tf adjustment input colname

        for level in comparison_levels:
            if not level._is_exact_match:
                continue
            colnames = level._exact_match_colnames
            if len(colnames) != 1:
                continue
            if colnames[0] == self._tf_adjustment_input_column_name.lower():
                return level.u_probability
        raise ValueError(
            "Could not find an exact match level for "
            f"{self._tf_adjustment_input_column_name}."
            "\nAn exact match level is required to make a term frequency adjustment "
            "on a comparison level that is not an exact match."
        )

    def _bayes_factor_sql(self, gamma_column_name: str):
        bayes_factor = (
            self._bayes_factor if self._bayes_factor != math.inf else "'Infinity'"
        )
        sql = f"""
        WHEN
        {gamma_column_name} = {self._comparison_vector_value}
        THEN cast({bayes_factor} as float8)
        """
        return dedent(sql)

    def _tf_adjustment_sql(
        self, gamma_column_name: str, comparison_levels: list[ComparisonLevel]
    ):
        gamma_colname_value_is_this_level = (
            f"{gamma_column_name} = {self._comparison_vector_value}"
        )

        # A tf adjustment of 1D is a multiplier of 1.0, i.e. no adjustment
        if self._comparison_vector_value == -1:
            sql = f"WHEN  {gamma_colname_value_is_this_level} then cast(1 as float8)"
        elif not self._has_tf_adjustments:
            sql = f"WHEN  {gamma_colname_value_is_this_level} then cast(1 as float8)"
        elif self._tf_adjustment_weight == 0:
            sql = f"WHEN  {gamma_colname_value_is_this_level} then cast(1 as float8)"
        elif self._is_else_level:
            sql = f"WHEN  {gamma_colname_value_is_this_level} then cast(1 as float8)"
        else:
            tf_adj_col = self._tf_adjustment_input_column

            coalesce_l_r = f"coalesce({tf_adj_col.tf_name_l}, {tf_adj_col.tf_name_r})"
            coalesce_r_l = f"coalesce({tf_adj_col.tf_name_r}, {tf_adj_col.tf_name_l})"

            tf_adjustment_exists = f"{coalesce_l_r} is not null"
            u_prob_exact_match = self._u_probability_corresponding_to_exact_match(
                comparison_levels
            )

            # Using coalesce protects against one of the tf adjustments being null
            # Which would happen if the user provided their own tf adjustment table
            # That didn't contain some of the values in this data

            # In this case rather than taking the greater of the two, we take
            # whichever value exists

            if self._tf_minimum_u_value == 0.0:
                divisor_sql = f"""
                (CASE
                    WHEN {coalesce_l_r} >= {coalesce_r_l}
                    THEN {coalesce_l_r}
                    ELSE {coalesce_r_l}
                END)
                """
            else:
                # This sql works correctly even when the tf_minimum_u_value is 0.0
                # but is less efficient to execute, hence the above if statement
                divisor_sql = f"""
                (CASE
                    WHEN {coalesce_l_r} >= {coalesce_r_l}
                    AND {coalesce_l_r} > cast({self._tf_minimum_u_value} as float8)
                        THEN {coalesce_l_r}
                    WHEN {coalesce_r_l}  > cast({self._tf_minimum_u_value} as float8)
                        THEN {coalesce_r_l}
                    ELSE cast({self._tf_minimum_u_value} as float8)
                END)
                """

            sql = f"""
            WHEN  {gamma_colname_value_is_this_level} then
                (CASE WHEN {tf_adjustment_exists}
                THEN
                POW(
                    cast({u_prob_exact_match} as float8) /{divisor_sql},
                    cast({self._tf_adjustment_weight} as float8)
                )
                ELSE cast(1 as float8)
                END)
            """
        return dedent(sql).strip()

    def as_dict(self):
        "The minimal representation of this level to use as an input to Splink"
        output = {}

        output["sql_condition"] = self.sql_condition

        if self.label_for_charts:
            output["label_for_charts"] = self.label_for_charts

        if self._m_probability and self._m_is_trained:
            output["m_probability"] = self.m_probability

        if self._u_probability and self._u_is_trained:
            output["u_probability"] = self.u_probability

        if self._has_tf_adjustments:
            output["tf_adjustment_column"] = self._tf_adjustment_input_column.input_name
            if self._tf_adjustment_weight != 0:
                output["tf_adjustment_weight"] = self._tf_adjustment_weight

        if self.is_null_level:
            output["is_null_level"] = True

        return output

    def _as_completed_dict(self):
        comp_dict = self.as_dict()
        comp_dict["comparison_vector_value"] = self._comparison_vector_value
        return comp_dict

    def _as_detailed_record(
        self, comparison_num_levels: int, comparison_levels: list[ComparisonLevel]
    ):
        "A detailed representation of this level to describe it in charting outputs"
        output = {}
        output["sql_condition"] = self.sql_condition
        output["label_for_charts"] = self._label_for_charts_no_duplicates(
            comparison_levels
        )

        output["m_probability"] = self.m_probability
        output["u_probability"] = self.u_probability

        output["m_probability_description"] = self._m_probability_description
        output["u_probability_description"] = self._u_probability_description

        output["has_tf_adjustments"] = self._has_tf_adjustments
        if self._has_tf_adjustments:
            output["tf_adjustment_column"] = self._tf_adjustment_input_column.input_name
        else:
            output["tf_adjustment_column"] = None
        output["tf_adjustment_weight"] = self._tf_adjustment_weight

        output["is_null_level"] = self.is_null_level
        output["bayes_factor"] = self._bayes_factor
        output["log2_bayes_factor"] = self._log2_bayes_factor
        output["comparison_vector_value"] = self._comparison_vector_value
        output["max_comparison_vector_value"] = comparison_num_levels - 1
        output["bayes_factor_description"] = self._bayes_factor_description

        return output

    def _parameter_estimates_as_records(
        self, comparison_num_levels: int, comparison_levels: list[ComparisonLevel]
    ):
        output_records = []

        cl_record = self._as_detailed_record(comparison_num_levels, comparison_levels)
        trained_values = self._trained_u_probabilities + self._trained_m_probabilities
        for trained_value in trained_values:
            record = {}
            record["m_or_u"] = trained_value["m_or_u"]
            p = trained_value["probability"]
            record["estimated_probability"] = p
            record["estimate_description"] = trained_value["description"]
            if p is not None and p != LEVEL_NOT_OBSERVED_TEXT and p > 0.0 and p < 1.0:
                record["estimated_probability_as_log_odds"] = math.log2(p / (1 - p))
            else:
                record["estimated_probability_as_log_odds"] = None

            record["sql_condition"] = cl_record["sql_condition"]
            record["comparison_level_label"] = cl_record["label_for_charts"]
            record["comparison_vector_value"] = cl_record["comparison_vector_value"]
            output_records.append(record)

        return output_records

    def _validate(self):
        self._validate_sql()

    def _abbreviated_sql(self, cutoff=75):
        sql = self.sql_condition
        return (sql[:cutoff] + "...") if len(sql) > cutoff else sql

    def __repr__(self):
        return f"<{self._human_readable_succinct}>"

    @property
    def _human_readable_succinct(self):
        sql = self._abbreviated_sql(75)
        return f"Comparison level '{self.label_for_charts}' using SQL rule: {sql}"

    @property
    def human_readable_description(self):
        input_cols = join_list_with_commas_final_and(
            [c.name for c in self._input_columns_used_by_sql_condition]
        )
        desc = (
            f"Comparison level: {self.label_for_charts} of {input_cols}\n"
            "Assesses similarity between pairwise comparisons of the input columns "
            f"using the following rule\n{self.sql_condition}"
        )

        return desc
