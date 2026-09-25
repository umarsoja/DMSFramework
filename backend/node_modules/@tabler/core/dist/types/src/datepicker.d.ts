/**
 * --------------------------------------------------------------------------
 * Tabler datepicker.ts
 * Licensed under MIT (https://github.com/tabler/tabler/blob/dev/LICENSE)
 * --------------------------------------------------------------------------
 */
import type { Calendar, DateAny, DateMode, DatesArr, MonthsCount, Options, PositionToInput, WeekDayID } from 'vanilla-calendar-pro';
import BaseComponent from './bootstrap/base-component';
import type { ElementSelector } from './bootstrap/types';
type ComponentConfig = {
    /** 'light', 'dark' or 'auto' for the popup only; null inherits from the nearest `[data-bs-theme]` */
    datepickerTheme: string | null;
    dateMin: DateAny | null;
    dateMax: DateAny | null;
    /** `Intl.DateTimeFormat` options, or a function(date, locale) returning the text shown in the field */
    dateFormat: Intl.DateTimeFormatOptions | ((date: Date, locale: string | undefined) => string) | null;
    /** element that shows the formatted date; a button uses itself or its `[data-bs-datepicker-display]` child */
    displayElement: string | HTMLElement | boolean | null;
    displayMonthsCount: MonthsCount;
    /** 0 = Sunday, 1 = Monday */
    firstWeekday: WeekDayID;
    /** render the calendar in place instead of a popup */
    inline: boolean;
    locale: string;
    /** element the popup is aligned with; defaults to the `.input-icon` / `.input-group` wrapper or the element */
    positionElement: string | HTMLElement | null;
    /** preselected dates as `YYYY-MM-DD` */
    selectedDates: string[];
    selectionMode: DateMode;
    placement: PositionToInput;
    /** pass-through for any Vanilla Calendar Pro option */
    vcpOptions: Options;
};
type ComponentConfigInput = Partial<ComponentConfig> & Record<string, unknown>;
type ChangeEventArgs = {
    dates: string[];
    event: MouseEvent;
};
/**
 * Class definition
 *
 * Wraps Vanilla Calendar Pro (https://vanilla-calendar.pro), loaded separately
 * as `window.VanillaCalendarPro`. Without the plugin the component is inert.
 * Same options, methods and events as Bootstrap 6's Datepicker, so markup
 * written for one works with the other.
 */
declare class Datepicker extends BaseComponent {
    _element: HTMLElement & {
        value: string;
    };
    _config: ComponentConfig;
    _calendar: Calendar | null;
    _isShown: boolean;
    _isShowing: boolean;
    _isHiding: boolean;
    _isSilent: boolean;
    _skipPluginShow: boolean;
    _resolveShown: (() => void) | null;
    _isInput: boolean;
    _isInline: boolean;
    _boundInput: HTMLInputElement | null;
    _positionElement: HTMLElement | null;
    _displayElement: HTMLElement | false | null;
    _themeObserver: MutationObserver | null;
    _onFocusIn: ((event: Event) => void) | null;
    _selectedDates: string[];
    constructor(element: ElementSelector, config?: ComponentConfigInput);
    static get Default(): ComponentConfig;
    static get DefaultType(): Record<keyof ComponentConfig, string>;
    static get NAME(): string;
    /** The Vanilla Calendar Pro instance, for options the component does not expose. */
    get calendar(): Calendar | null;
    toggle(): Promise<void>;
    show(): Promise<void>;
    hide(): Promise<void>;
    dispose(): void;
    getSelectedDates(): string[];
    setSelectedDates(dates: DatesArr): void;
    _initCalendar(): void;
    _updateDisplayWithSelectedDates(): void;
    _writeSelection(selectedDates: string[]): void;
    _resolvePositionElement(): HTMLElement;
    _resolveDisplayElement(): HTMLElement | false | null;
    _alignToPositionElement(): void;
    _getThemeAncestor(): Element | null;
    _getEffectiveTheme(): string | null;
    _syncThemeAttribute(element: HTMLElement | undefined): void;
    _setupThemeObserver(): void;
    _setupDismissOnFocus(): void;
    _silently(callback: () => void): void;
    _handlePluginShow(): void;
    _handlePluginHide(): void;
    _buildCalendarOptions(): Options;
    _handleDateClick(self: Calendar, event: MouseEvent): void;
    _maybeHideAfterSelection(selectedDates: string[]): void;
    _parseDate(dateStr: string): Date;
    _formatDate(dateStr: string): string;
    _formatDateForInput(dates: string[]): string;
    _parseInputValue(): void;
}
export default Datepicker;
export type { ComponentConfig as DatepickerConfig, ChangeEventArgs as DatepickerChangeEventArgs };
