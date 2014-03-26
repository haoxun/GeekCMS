
"""
Syntax:
    start       : NEWLINE lines end
                | lines end

    end         : plugin_expr
                | empty

    lines       : lines line_atom
                | empty

    line_atom   : plugin_expr NEWLINE

    plugin_expr : plugin_name relation plugin_name
                | plugin_name relation
                | relation plugin_name
                | plugin_name

    relation    : left_rel
                | right_rel

    left_rel    : LEFT_OP
                | LEFT_OP DEGREE

    right_rel   : RIGHT_OP
                | DEGREE RIGHT_OP

    plugin_name : IDENTIFIER

    empty       : <empty>

Semantics:
    1. "pre_load: my_loader": register plugin "my_loader" to component
    "pre_load".
    2. "pre_load: my_loader << my_filter": register plugins "my_loader" and
    "my_filter" to component "pre_load", with "my_loader" being executed before
    "my_filter".
    3. "pre_load: my_filter >> my_loader": has the same meaning as
    "pre_load: my_loader << my_filter".
    4. "pre_load: loader_a <<0 loader_b NEWLINE loader_c <<1 loader_b" the
    execution order would be "loader_c" --> "loader_a" --> "loader_b".
    "<<" is equivalent to "<<0", and "<< decimalinteger" is equivalent to
    "decimalinteger >>".
    5. "pre_load: my_loader <<": means "my_loader" would be executed before the
    other plugins within a component, unless another relation such as
    "anther_loader <<1" is established.
    6. "pre_load: >> my_filter": reverse meaning of "pre_load: my_loader <<".

Algorithm:
    1. lexical analysis and Syntax Checking: Performed by PLY, extract plugin
    relation expression from each physical line, transform to the format of
    'x <<p y'. Some important syntax directed actions are as follow:
        1.1 Extract left operand, operator and right operand.
        1.2 If x is missed, HEAD is added as x; If y is missed, TAIL is added
        as y;
        1.3 For expressions that only consist of one operand and no operator,
        for example, 'x NEWLINE', the only operand in the expression would be
        considered as the left operand, with no relation and right operand.
        1.4 '<<' is transform to '<<0', and so '>>'.
    2. Preparation for generating plugin execution order.
        2.1 Transform operand to the form of (theme, plugin), based on
        'theme.plugin'. If 'theme.' part is omitted, then automatically
        generate theme with respect to file's directory(where relation
        expressions were loaded).
        2.2 Expressions that has left operand with no relation and right
        operand, would be removed and kept in somewhere else. Such expressions
        would not be used to generating relation group(step 3).
        2.3 Transform 'x p>> y' to 'y <<p x'.
    3. Generate relation groups.
    A relation group: {(x <<p y)| for x, all avaliable (p, y) in expressions}.
        3.1 Sort expressions(x <<p y) with respect to x's value, then with p's
        value. Generate raw relation groups. Then sort expressions(x <<p y)
        with respect to y's value, then with p's value in reversed order.
        3.2 For every raw relation groups, tranlate all its relations
        (x <<p1 y1, x <<p2 y2, ..., x <<pn yn) to (x < y1, y1 < y2, ...,
        yn-1 < yn) and (xn <<pn y, xn-1 <<pn-1 y, ..., x1 <<p1 y) to
        (xn < xn-1, xn-1 < xn-2, ..., x1 < y). Notice that 'x < y' means 'x is
        executed earlier then y', in order to distingush with '<<', since
        'x << y1, x << y2' would cause syntax error.
    4. Generate order of plugin execution.
        Input: relations generated by 3.2.
        Output: sequence of plugin execution.

        order = a queue
        left_behind = a set initiated with items removed in 2.3.

        left_hand_side = the dict of left operands, with index as its key and
        reference count as its value.
        right_hand side = the dict of right operands, similar with
        left_hand_side.

        left_behind = items only in right_hand_side.
        while left_hand_side:
            find x in left_hand_side and not in right hand side. If such x not
            exist, then there must be a syntax error.
            remove x from left_hand_side.
            for y of all relations (x < y):
                decrease y's reference count by 1.
                if y's reference count equals to zero, delete y from
                right_hand_side.

        if left_behind is not empty, them push all its items to order.
        return order
    5. Remove HEAD and TAIL from order.
"""

import inspect
import functools
from collections import defaultdict

from .parser.simple_lex import lexer
from .parser.simple_yacc import parser
from .parser.utils import ErrorCollector
from .parser.utils import PluginExpr
from .parser.utils import PluginRel
from .protocol import PluginIndex


_SPECIAL_DEGREE = -1


class _Algorithm:

    def __init__(self, exprs):
        self._exprs = []
        for container in exprs:
            self._exprs.extend(container)

    # implement 2.3
    def _transform_to_left_rel(self):
        for expr in self._exprs:
            relation = expr.relation
            if relation.is_left_rel:
                continue
            relation.is_left_rel = True
            # exchange operand
            expr.left_operand, expr.right_operand =\
                expr.right_operand, expr.left_operand

    # implement 2.2
    def _remove_irrelevant_exprs(self):

        irrelevant_exprs = []
        for expr in self._exprs[:]:
            # if expr.relation is None, then expr is so called irrelevant.
            if expr.relation is None:
                self._exprs.remove(expr)
                irrelevant_exprs.append(expr)
        return irrelevant_exprs

    def _yield_group(self, sorted_exprs, op_name):
        while sorted_exprs:
            group = []
            val = getattr(sorted_exprs[0], op_name)
            while sorted_exprs\
                    and val == getattr(sorted_exprs[0], op_name):
                expr = sorted_exprs.pop(0)
                group.append(expr)
            yield group

    # implement 3.1.1
    def _generate_left_relation_group(self, exprs):
        cmp_key_left = lambda x: (hash(x.left_operand), x.relation.degree)
        sorted_exprs_left = sorted(exprs, key=cmp_key_left)
        # group by left operand.
        for group in self._yield_group(sorted_exprs_left, 'left_operand'):
            yield group

    # implement 3.1.2
    def _generate_right_relation_group(self, exprs):
        cmp_key_right = lambda x: (hash(x.right_operand), x.relation.degree)
        sorted_exprs_right = sorted(exprs, key=cmp_key_right, reverse=True)
        # group by right operand.
        for group in self._yield_group(sorted_exprs_right, 'right_operand'):
            yield group

    def _break_relation_group(self, relation_group, op_name, special_index):
        """
        op_name is the string of operand NOT to be gathered.
        """
        new_group = []
        special_rel = PluginRel(True, _SPECIAL_DEGREE)

        last_operand = None
        for expr in relation_group:

            if last_operand is None:
                # set up last_operand
                last_operand = getattr(expr, op_name)
                continue

            combined_expr = PluginExpr(
                left_operand=last_operand,
                right_operand=getattr(expr, op_name),
                relation=special_rel,
            )
            new_group.append(combined_expr)
            # update last_operand
            last_operand = getattr(expr, op_name)

        special_expr = relation_group[special_index]
        new_expr = PluginExpr(
            left_operand=special_expr.left_operand,
            right_operand=special_expr.right_operand,
            relation=special_rel,
        )
        if special_index == 0:
            new_group.insert(0, new_expr)
        elif special_index == -1:
            new_group.append(new_expr)
        else:
            raise SyntaxError

        return new_group

    # implement 3.2.1
    def _break_left_relation_group(self, relation_group):
        return self._break_relation_group(relation_group, 'right_operand', 0)

    # implement 3.2.2
    def _break_right_relation_group(self, relation_group):
        return self._break_relation_group(relation_group, 'left_operand', -1)

    # implement 4 and 5
    def _generate_execution_order(self, relations, irrelevant_exprs):
        order = []
        left_behind = {expr.left_operand for expr in irrelevant_exprs}

        left_hand_side = defaultdict(list)
        right_hand_side = defaultdict(int)
        for expr in relations:
            left_hand_side[expr.left_operand].append(expr.right_operand)
            right_hand_side[expr.right_operand] += 1

        items_only_in_right_hand_side =\
            set(right_hand_side.keys()) - set(left_hand_side.keys())
        left_behind |= items_only_in_right_hand_side

        while left_hand_side:
            unique_items =\
                set(left_hand_side.keys()) - set(right_hand_side.keys())

            if not unique_items:
                text = "Something Wrong. LHS: '{}' RHS: '{}'"
                raise SyntaxError(
                    text.format(dict(left_hand_side), dict(right_hand_side)),
                )

            item = unique_items.pop()
            order.append(item)

            for right_op in left_hand_side[item]:
                right_hand_side[right_op] -= 1
                if right_hand_side[right_op] == 0:
                    del right_hand_side[right_op]
            del left_hand_side[item]

        order.extend(left_behind)

        # remove HEAD and TAIL
        HEAD_AND_TAIL = [PluginExpr.HEAD, PluginExpr.TAIL]
        for index in order[:]:
            if index.theme_name is None\
                    and index.plugin_name in HEAD_AND_TAIL:
                order.remove(index)

        return order

    # Mix up all above functions.
    def generate_sequence(self):
        irrelevant_exprs = self._remove_irrelevant_exprs()
        self._transform_to_left_rel()

        new_relations = []
        # left operand.
        for relation_group in self._generate_left_relation_group(self._exprs):
            new_group = self._break_left_relation_group(relation_group)
            new_relations.extend(new_group)
        # right operand.
        for relation_group in self._generate_right_relation_group(self._exprs):
            new_group = self._break_right_relation_group(relation_group)
            new_relations.extend(new_group)

        return self._generate_execution_order(new_relations, irrelevant_exprs)


class SequenceParser:

    def __init__(self):
        self.error = False
        self.theme_plugin_expr_mapping = dict()
        # bind parser with lexer
        self._parse = functools.partial(parser.parse, lexer=lexer)

    # implement 2.2
    def _replace_with_plugin_index(self, theme, plugin_exprs):

        def get_theme_plugin(operand):
            # special case
            if operand == PluginExpr.HEAD or operand == PluginExpr.TAIL:
                return None, operand
            # return theme.plugin or plugin.
            items = operand.split('.')
            if len(items) == 1:
                return theme, operand
            elif len(items) == 2:
                return items
            else:
                raise SyntaxError('Operand Error: {}'.format(operand))

        processed_exprs = []
        for expr in plugin_exprs:

            left_theme, left_plugin = get_theme_plugin(expr.left_operand)
            right_theme, right_plugin = get_theme_plugin(expr.right_operand)

            left_index = PluginIndex(left_theme, left_plugin)
            right_index = PluginIndex(right_theme, right_plugin)

            new_expr = PluginExpr(
                left_operand=left_index,
                right_operand=right_index,
                relation=expr.relation,
            )
            processed_exprs.append(new_expr)

        return processed_exprs

    def _archive_error(self):
        if ErrorCollector.lex_error:
            self.error = True
            ErrorCollector.archive_lex_messages(theme)

        if ErrorCollector.yacc_error:
            self.error = True
            ErrorCollector.archive_yacc_messages(theme)

    def analyze(self, theme, text):
        exprs = self._parse(text)
        self._archive_error()

        processed_exprs = self._replace_with_plugin_index(theme, exprs)
        self.theme_plugin_expr_mapping[theme] = processed_exprs

    def report_error(self):
        # print lex error
        for theme, messages in ErrorCollector.theme_lex_error.items():
            # lineno not really the line number of 'settings' file.
            # might be improved in the future.
            val, lineno = messages
            template = "Theme '{}' >> Illegal Character: '{}' in line {}"
            print(template.format(theme, val, lineno))

        # print yacc error
        for theme, messages in ErrorCollector.theme_yacc_error.items():
            val, lineno, discard = messages
            template = ("Theme '{}' >> Syntax Error: '{}' in line {}"
                        "Discard: {}")
            print(template.format(theme, val, lineno, discard))

    def generate_sequence(self):
        algorithm = _Algorithm(
            self.theme_plugin_expr_mapping.values(),
        )
        return algorithm.generate_sequence()
