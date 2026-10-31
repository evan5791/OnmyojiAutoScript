"""Local account-flow rules, consumed by the existing OAS recognition engine."""
from module.atom.image import RuleImage
from module.atom.ocr import RuleOcr
from module.atom.click import RuleClick


def image(name, front, back, threshold=.85):
    return RuleImage(roi_front=front, roi_back=back, threshold=threshold,
                     method='Template matching', file='./tasks/AccountSwitch/assets/' + name + '.png')


def ocr(name, roi):
    return RuleOcr(roi=roi, area=roi, mode='Full', method='Default', keyword='', name=name)


class AccountFlowAssets:
    I_EXIT = RuleImage(roi_front=(14,12,43,41),roi_back=(0,0,91,83),threshold=.8,
                       method='Template matching',file='./tasks/Component/GeneralBattle/gb/gb_exit.png')
    I_EXIT_ENSURE = RuleImage(roi_front=(699,414,107,46),roi_back=(662,364,250,121),threshold=.8,
                              method='Template matching',file='./tasks/AccountSwitch/assets/assist_exit_ensure.png')
    I_ASSIST_SELECTED = image('alt_assist_selected_check', (595,362,39,40), (450,315,700,260))
    I_ASSIST_DEPLOYED = image('alt_assist_deployed_weapons', (341,508,36,30), (240,495,850,65))
    I_ASSIST_PREP_HINT = image('alt_assist_prepare_hint', (511,688,260,26), (440,670,470,45), .82)
    I_ACCOUNT_EXIT_GAME_TITLE = image('i_alt_exit_game_title', (404,265,135,48), (390,245,170,80), .88)
    I_ACCOUNT_EXIT_GAME_CANCEL = image('i_alt_exit_game_cancel', (773,412,70,56), (700,395,300,85), .88)
    I_ACCOUNT_EXIT_BATTLE_TITLE = image('i_alt_exit_battle_title', (531,294,220,38), (480,270,320,85), .88)
    I_ACCOUNT_EXIT_BATTLE_CANCEL = image('i_alt_exit_battle_cancel', (436,408,180,64), (400,380,500,120), .88)
    O_ASSIST_PANEL = ocr('account_assist_panel', (750,85,400,170))
    O_ASSIST_TAB = ocr('account_assist_tab', (1180,510,100,140))
    O_ASSIST_FRIENDS_TITLE = ocr('account_friends_title', (550,35,180,65))
    O_ASSIST_CARDS = ocr('account_assist_cards', (450,335,700,290))
    O_ASSIST_PICKER = ocr('account_assist_picker', (240,505,850,183))
    C_ASSIST_OPEN_PICKER = RuleClick(roi_front=(300,520,40,40), roi_back=(300,520,40,40), name='account_assist_open_picker')
